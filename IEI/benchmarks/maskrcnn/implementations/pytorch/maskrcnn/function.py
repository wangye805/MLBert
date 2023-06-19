# Copyright (c) 2018-2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# python function.py [--graph-after-ddp] [--graph-before-ddp]
# python -m torch.distributed.launch --nproc_per_node=2 function.py [--graph-after-ddp] [--graph-before-ddp]

import torch
TORCH_MAJOR = int(torch.__version__.split('.')[0])
TORCH_MINOR = int(torch.__version__.split('.')[1])
if TORCH_MAJOR == 1 and TORCH_MINOR < 8:
    from torch._six import container_abcs
else:
    import collections.abc as container_abcs
import types
from itertools import chain
import argparse
import os

# questions:
# is a custom autograd function or graphing around a backward call better?
# how to allow double backward?
# lazily capture as part of live backward, or not?
# capture all the way down to AccumulateGrad functions, or not?
# If yes, need to deal with params used in graphs and non-graphed regions,
# and DDP bucket-slot-ready flags.  To help, user could supply a list of params
# known to be exclusive to the graphed region.

# Current limitation:  Assumes all args are Tensors.
# Arg tensors may or may not require grad.
# Any temporaries created in func_or_module must not be used
# outside func_or_module unless they are among func_or_module's
# explicit return values.

def graph(func_or_module,
          sample_args,
          graph_stream=None,
          warmup_iters=2,
          warmup_only=False,
          overwrite_fn='forward',
          return_pool_id=False,
          use_pool_id=None):

    assert isinstance(sample_args, tuple)

    # def filter_tensors(args, filtered):
    #     for arg in args:
    #         if isinstance(arg, torch.Tensor):
    #             filtered.append(args)
    #         elif isinstance(arg, container_abcs.Iterable):
    #             filter_tensors(arg, filtered)

    # To run a module's forward method as a torch.autograd.Function,
    # and ensure gradients of all used tensors are returned by the Function's backward
    # so the autograd engine takes care of final accumulation (which includes DDP hooks)
    # we need to "functionalize" module.forward:
    # createa a wrapper function where module attributes
    # and user args all enter through the arglist.
    was_module = isinstance(func_or_module, torch.nn.Module)
    if was_module:
        if isinstance(func_or_module, torch.nn.parallel.DistributedDataParallel):
            func_or_module = func_or_module.module
        module_params = tuple(func_or_module.parameters())
        functional_args = sample_args + module_params

    stream = torch.cuda.Stream() if graph_stream is None else graph_stream
    ambient_stream = torch.cuda.current_stream()
    stream.wait_stream(ambient_stream)

    # Most of the spaghetti here comes from handling args that may not require grad.

    with torch.cuda.stream(stream):
        # warmup iters before capture
        for _ in range(warmup_iters):
            # Warmup iters should warm up the same memory pool capture will use.  If they don't,
            # and we use the capture pool for the first time during capture, we'll almost
            # certainly capture some cudaMallocs.
            outputs  = func_or_module(*sample_args)

            outputs_was_tensor = isinstance(outputs, torch.Tensor)
            outputs = (outputs,) if outputs_was_tensor else outputs

            outputs_require_grad = tuple(o for o in outputs if o.requires_grad)
            args_require_grad = tuple(i for i in functional_args if i.requires_grad)
            buffer_incoming_grads = tuple(torch.empty_like(o) if o.requires_grad else None for o in outputs)
            needed_incoming_grads = tuple(b for b in buffer_incoming_grads if b is not None)
            grad_inputs = torch.autograd.grad(outputs_require_grad,
                                              args_require_grad,
                                              needed_incoming_grads,
                                              only_inputs=True,
                                              allow_unused=False)
        if warmup_iters > 0:
            del outputs, outputs_require_grad, args_require_grad, buffer_incoming_grads, needed_incoming_grads, grad_inputs
        if warmup_only:
            ambient_stream.wait_stream(stream)
            if was_module:
                setattr(func_or_module, overwrite_fn, func_or_module.forward)
            return func_or_module

        print("Graphing\n", flush=True)

        # Capture forward pass
        fwd_graph = torch.cuda.CUDAGraph()
        if use_pool_id is not None:
            fwd_graph.capture_begin(pool=use_pool_id)
        else:
            fwd_graph.capture_begin()
        outputs  = func_or_module(*sample_args)
        fwd_graph.capture_end()

        outputs_was_tensor = isinstance(outputs, torch.Tensor)
        outputs = (outputs,) if outputs_was_tensor else outputs

        outputs_require_grad = tuple(o for o in outputs if o.requires_grad)
        args_require_grad = tuple(i for i in functional_args if i.requires_grad)
        buffer_incoming_grads = tuple(torch.empty_like(o) if o.requires_grad else None for o in outputs)
        needed_incoming_grads = tuple(b for b in buffer_incoming_grads if b is not None)

        # Capture gradient creation
        bwd_graph = torch.cuda.CUDAGraph()
        bwd_graph.capture_begin(pool=fwd_graph.pool())
        grad_inputs = torch.autograd.grad(outputs_require_grad,
                                          args_require_grad,
                                          needed_incoming_grads,
                                          only_inputs=True,
                                          allow_unused=False)
        bwd_graph.capture_end()

        buffer_inputs = tuple(i.detach() for i in functional_args)
        buffer_outputs = tuple(o.detach().requires_grad_(o.requires_grad) for o in outputs)

        # Constructs a list suitable for returning from Graphed.backward:
        # Inserts Nones in gradient slots for inputs that don't expect a grad.
        buffer_grad_inputs = []
        grad_idx = 0
        for arg in functional_args:
            if arg.requires_grad:
                buffer_grad_inputs.append(grad_inputs[grad_idx])
                grad_idx += 1
            else:
                buffer_grad_inputs.append(None)
        buffer_grad_inputs = tuple(buffer_grad_inputs)

    ambient_stream.wait_stream(stream)

    class Graphed(torch.autograd.Function):
        @staticmethod
        def forward(ctx, *inputs):
            with torch.no_grad():
                for i, arg in zip(buffer_inputs, inputs):
                    if i.data_ptr() != arg.data_ptr():
                        i.copy_(arg)
            fwd_graph.replay()
            return buffer_outputs
        @staticmethod
        def backward(ctx, *grads):
            with torch.no_grad():
                for g, grad in zip(buffer_incoming_grads, grads):
                    if g is not None:
                        g.copy_(grad)
            bwd_graph.replay()
            return tuple(b.detach() if b is not None else b for b in buffer_grad_inputs)

    if was_module:
        def functionalized(self, *user_args):
            out = Graphed.apply(*(user_args + module_params))
            return out[0] if outputs_was_tensor else out
        setattr(func_or_module, overwrite_fn, types.MethodType(functionalized, func_or_module))
        if return_pool_id:
            return func_or_module, fwd_graph.pool()
        else:
            return func_or_module
    else:
        return Graphed.apply


def main():
    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    parser.add_argument("--local_rank", default=0, type=int)
    parser.add_argument("--graph-before-ddp", action="store_true")
    parser.add_argument("--graph-after-ddp", action="store_true")
    args = parser.parse_args()

    args.distributed = False
    if 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1

    args.gpu = 0
    args.world_size = 1

    if args.distributed:
        args.gpu = args.local_rank
        torch.cuda.set_device(args.gpu)
        torch.distributed.init_process_group(backend='nccl',
                                             init_method='env://')
        args.world_size = torch.distributed.get_world_size()

    torch.backends.cudnn.benchmark = False
    torch.manual_seed(args.local_rank + 1)
    torch.cuda.manual_seed(args.local_rank + 1)

    print("{} graph_before_ddp {} graph_after_ddp {}\n".format(args.local_rank,
                                                               args.graph_before_ddp,
                                                               args.graph_after_ddp),
          flush=True)

    N, D_in, H, D_out = 640, 4096, 2048, 1024

    stream = torch.cuda.Stream()

    model_segment1 = torch.nn.Sequential(torch.nn.Linear(D_in, H),
                                torch.nn.Dropout(p=0.2),
                                torch.nn.Dropout(p=0.4)).cuda()

    model_segment2 = torch.nn.Sequential(torch.nn.Linear(H, D_out),
                                torch.nn.Dropout(p=0.3),
                                torch.nn.Dropout(p=0.1)).cuda()

    loss_fn = torch.nn.MSELoss()

    optimizer = torch.optim.SGD(chain(model_segment1.parameters(),
                                      model_segment2.parameters()),
                                lr = 0.1)

    x = torch.randn(N, D_in, device='cuda')
    y = torch.randn(N, D_out, device='cuda')

    pure_eager = not (args.graph_before_ddp or args.graph_after_ddp)

    if pure_eager:
        print("Calling graph() pure eager\n")
        model_segment1 = graph(model_segment1,
                               (x.clone(),),
                               stream,
                               warmup_only=True)
    elif args.graph_before_ddp:
        print("Calling graph() before ddp\n")

        model_segment1 = graph(model_segment1,
                               (x.clone(),),
                               stream,
                               warmup_iters=0,
                               warmup_only=True,
                               overwrite_fn='eager_forward')
        model_segment1 = graph(model_segment1,
                               (x.clone(),),
                               stream,
                               warmup_only=False,
                               overwrite_fn='graph_forward')

        class GraphedWrapper(torch.nn.Module):
            def __init__(self, model_segment):
                super().__init__()
                self.model_segment = model_segment
            def forward(self, x):
                if self.training:
                    return self.model_segment.graph_forward(x)
                else:
                    return self.model_segment.eager_forward(x)

        model_segment1 = GraphedWrapper(model_segment1)

    model = torch.nn.Sequential(model_segment1, model_segment2)
    if args.distributed:
        # Small bucket cap to stress DDP
        torch.cuda.nvtx.range_push("DDP")
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          bucket_cap_mb=1,
                                                          device_ids=[args.local_rank],
                                                          gradient_as_bucket_view=True)
        torch.cuda.nvtx.range_pop()

    if args.graph_after_ddp:
        if args.distributed:
            print("Calling graph() after ddp\n")
            model.module[0] = graph(model.module[0], (x.clone(),), stream)
        else:
            model[0] = graph(model_segment1, (x.clone(),), stream)

    for e in range(2):
        model.train()
        for i in range(10):
            torch.cuda.nvtx.range_push("{}".format(i))
            optimizer.zero_grad(set_to_none=True)

            y_pred = model(x)
            loss = loss_fn(y_pred, y)
            torch.cuda.nvtx.range_push("backward")
            loss.backward()
            torch.cuda.nvtx.range_pop()

            # possibly needed if post-backward sync is commented out in pytorch
            # torch.cuda.synchronize()

            torch.cuda.nvtx.range_push("step")
            optimizer.step()
            torch.cuda.nvtx.range_pop()
            torch.cuda.nvtx.range_pop()

        print("train: {} {} {} {}".format(args.local_rank,
                                          loss.item(),
                                          tuple(p.grad.sum().item() for p in model_segment1.parameters()),
                                          tuple(p.grad.sum().item() for p in model_segment2.parameters())),
              flush=True)

        # do eval end of epoch
        with torch.no_grad():
            model.eval()
            y_pred = model(x)
            loss = loss_fn(y_pred, y)
        print("eval: {}\n".format(loss))

if __name__ == "__main__":
    main()
