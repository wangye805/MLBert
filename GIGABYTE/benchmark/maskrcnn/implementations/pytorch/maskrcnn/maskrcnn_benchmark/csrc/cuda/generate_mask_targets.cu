/**
 * Copyright (c) 2018-2023, NVIDIA CORPORATION. All rights reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#include <torch/extension.h>
#include <iostream>
#include <vector>
#include <cuda.h>
#include <cuda_runtime.h>
#include <ATen/ATen.h>
#include <ATen/cuda/CUDAContext.h>
#include <math.h>
#include <algorithm>
#include <stdlib.h>
#include "cpu/vision.h"


/*rle cuda kernels are cuda version of the corresponding cpu functions here 
https://github.com/cocodataset/cocoapi/blob/master/common/maskApi.c 
these are only a subset of rle kernels.*/

typedef unsigned int uint;
typedef unsigned long siz;
typedef unsigned char byte;

//6144 is based on minimum shared memory size per SM 
//across all pytorch-supported GPUs. Need to use blocking
//to avoid this restriction
const int BUFFER_SIZE=6144;
const int CNTS_SIZE=6144;

__global__ void create_poly_rel_idx_kernel(int* segmask_poly_idx, int* segmask_poly_rel_idx, int64_t* clamped_idxs, int num_anchors,
				           int* per_anchor_poly_idx, int* per_anchor_first_poly_idx,
				           int* per_anchor_poly_rel_idx)
{
    int anchor_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (anchor_idx < num_anchors)
    {
	int segmask_id = static_cast<int>(clamped_idxs[anchor_idx]);
	int segmask_first_poly_idx = segmask_poly_idx[segmask_id];
	int num_mask = segmask_poly_idx[segmask_id+1] - segmask_first_poly_idx;
	int poly_start_idx = per_anchor_poly_idx[anchor_idx];
	int poly_rel_start_idx = per_anchor_first_poly_idx[anchor_idx];
	for (int poly_id = 0;  poly_id < num_mask;  ++poly_id)
	    per_anchor_poly_rel_idx[poly_start_idx+poly_id] = poly_rel_start_idx + (segmask_poly_rel_idx[segmask_first_poly_idx+poly_id] - segmask_poly_rel_idx[segmask_first_poly_idx]);
    } else if (anchor_idx == num_anchors) {
        int poly_start_idx = per_anchor_poly_idx[anchor_idx];
        int poly_rel_start_idx = per_anchor_first_poly_idx[anchor_idx];
        per_anchor_poly_rel_idx[poly_start_idx] = poly_rel_start_idx;
    }
}

template<typename T_i, typename T_o>
__global__ void create_dense_poly_data_kernel(int* segmask_poly_idx, int* segmask_poly_rel_idx, int64_t* clamped_idxs,
		                              int* per_anchor_poly_idx, int* per_anchor_poly_rel_idx,
				              T_i* segmask_dense_poly_data, T_o* dense_poly_data)
{
    int anchor_idx = blockIdx.x;
    int segmask_id = static_cast<int>(clamped_idxs[anchor_idx]);
    int segmask_first_poly_idx = segmask_poly_idx[segmask_id];
    int segmask_first_poly_rel_idx = segmask_poly_rel_idx[segmask_first_poly_idx];
    T_i* src = segmask_dense_poly_data + segmask_first_poly_rel_idx;
    int poly_start_idx = per_anchor_poly_idx[anchor_idx];
    int poly_rel_start_idx = per_anchor_poly_rel_idx[poly_start_idx];
    T_o* dst = dense_poly_data + poly_rel_start_idx;
    int poly_end_idx = per_anchor_poly_idx[anchor_idx+1];
    int num_samples = per_anchor_poly_rel_idx[poly_end_idx] - poly_rel_start_idx;
    for (int i = threadIdx.x;  i < num_samples;  i+= blockDim.x) {
        dst[i] = static_cast<T_o>(src[i]);
    }
}

__global__ void crop_and_scale_cuda_kernel(double *dense_poly_data, int *per_anchor_poly_idx, int *poly_rel_idx, 
                                           int max_num_poly_per_anchor, float4 *anchor_data, int mask_size){
    int anchor_idx = blockIdx.x / max_num_poly_per_anchor;
    int anchor_poly_id = blockIdx.x - anchor_idx * max_num_poly_per_anchor;
    int num_polys_for_this_anchor = per_anchor_poly_idx[anchor_idx+1] - per_anchor_poly_idx[anchor_idx];
    if (anchor_poly_id >= num_polys_for_this_anchor)
	    return;  // no work for this block
    int poly_id = per_anchor_poly_idx[anchor_idx] + anchor_poly_id;

    int tid = threadIdx.x;
    int block_jump = blockDim.x;

    float w = anchor_data[anchor_idx].z - anchor_data[anchor_idx].x;
  	float h = anchor_data[anchor_idx].w - anchor_data[anchor_idx].y;
  	w = fmaxf(w, 1.0f);
  	h = fmaxf(h, 1.0f);  	
    float ratio_h = ((float) mask_size) / h;
    float ratio_w = ((float) mask_size) / w;

    int poly_ptr_idx_start = poly_rel_idx[poly_id];
    int poly_ptr_idx_end = poly_rel_idx[poly_id + 1];

    double *poly_data_buf = dense_poly_data + poly_ptr_idx_start;
    int len = poly_ptr_idx_end - poly_ptr_idx_start;
    
    for (int j = tid; j < len; j += block_jump){
        if (j % 2 == 0) poly_data_buf[j] = ratio_w*((float) poly_data_buf[j]- anchor_data[anchor_idx].x);
        if (j % 2 == 1) poly_data_buf[j] = ratio_h*((float) poly_data_buf[j]- anchor_data[anchor_idx].y);
    }
}

//merging masks happens on mask format, not RLE format.     
__global__ void merge_masks_cuda_kernel(byte *masks_in, float *masks_out, const int mask_size, 
                                        int *per_anchor_poly_idx){

    int anchor_idx = blockIdx.x;
    int tid = threadIdx.x;
    int jump_block = blockDim.x;
    int mask_start_idx = per_anchor_poly_idx[anchor_idx];
    int num_of_masks_to_merge = per_anchor_poly_idx[anchor_idx + 1]-per_anchor_poly_idx[anchor_idx];
    
    for(int j = tid; j < mask_size * mask_size; j += jump_block){
        int transposed_pixel = (j % mask_size) * mask_size + j / mask_size;
        byte pixel = 0;
        for(int k = 0; k < num_of_masks_to_merge; k++){
            if (masks_in[(mask_start_idx + k) * mask_size * mask_size + j] == 1) pixel = 1;
            if (pixel == 1) break;
        }
        masks_out[anchor_idx * mask_size * mask_size + transposed_pixel] = (float) pixel;       
    }
}

/*cuda version of rleDecode function in this API: 
https://github.com/cocodataset/cocoapi/blob/master/common/maskApi.c*/

__global__ void decode_rle_cuda_kernel(const int *num_of_cnts, uint *cnts, long h, long w, byte *mask, int *per_anchor_poly_idx, int max_num_poly_per_anchor)
{
    int anchor_idx = blockIdx.x / max_num_poly_per_anchor;
    int anchor_poly_id = blockIdx.x - anchor_idx * max_num_poly_per_anchor;
    int num_polys_for_this_anchor = per_anchor_poly_idx[anchor_idx+1] - per_anchor_poly_idx[anchor_idx];
    if (anchor_poly_id >= num_polys_for_this_anchor)
	    return;  // no work for this block
    int poly_id = per_anchor_poly_idx[anchor_idx] + anchor_poly_id;

    int tid = threadIdx.x;
    int block_jump = blockDim.x;
    
    int m = num_of_cnts[poly_id];
    uint *cnts_buf = cnts + CNTS_SIZE * poly_id;
    byte *mask_ptr = mask + poly_id * h * w;
    
    __shared__ uint shbuf1[CNTS_SIZE];
    __shared__ uint shbuf2[CNTS_SIZE];

    
    //initialize shbuf for scan. first element is 0 (exclusive scan)
    for (long i = tid; i < CNTS_SIZE; i += block_jump){
        shbuf1[i] = (i <= m & i > 0) ? cnts_buf[i - 1]:0;
        shbuf2[i] = (i <= m & i > 0) ? cnts_buf[i - 1]:0;
    }
    __syncthreads();
    
    //double buffering for scan
    int switch_buf = 0;
    for (int offset = 1; offset <= m; offset *= 2){
        switch_buf = 1 - switch_buf;
        if(switch_buf == 0){
            for(int j = tid;j <= m;j += block_jump){
                if(j >= offset) shbuf2[j] = shbuf1[j]+shbuf1[j - offset];
                else shbuf2[j] = shbuf1[j];
            }
        }else if (switch_buf == 1){
            for(int j = tid;j <= m;j += block_jump){
                if(j >= offset) shbuf1[j] = shbuf2[j] + shbuf2[j - offset];
                else shbuf1[j] = shbuf2[j];
            }        
        
        }
        __syncthreads();
    }
    uint *scanned_buf = switch_buf == 0 ? shbuf2 : shbuf1;
       
    //find which bin pixel j falls into , which determines the pixel value
    //use binary search
    for(int j = tid; j < h * w; j += block_jump){
        int min_idx = 0;
        int max_idx = m;
        int mid_idx = m / 2;        
        while(max_idx > min_idx){
            if(j > scanned_buf[mid_idx]) {
                min_idx = mid_idx+1; 
                mid_idx = (min_idx + max_idx) / 2;
            }
            else if (j < scanned_buf[mid_idx]) {
                max_idx = mid_idx; 
                mid_idx = (min_idx + max_idx) / 2;
            }
            else {
                mid_idx++; 
                break;
            }    
        }         
        int k = mid_idx;
        byte pixel = k % 2 == 0 ? 1 : 0;
        mask_ptr[j] = pixel; 
    }
}

/*cuda version of rleFrPoly function in this API: 
https://github.com/cocodataset/cocoapi/blob/master/common/maskApi.c*/

__global__ void rle_fr_poly_cuda_kernel(const double *dense_coordinates, int *poly_rel_idx, long h, long w, 
                                        uint *cnts, int *x_in, int *y_in, int *u_in, int *v_in, uint *a_in, 
                                        uint *b_in, int *num_of_cnts,
					int *per_anchor_poly_idx, int max_num_poly_per_anchor) {
    int anchor_idx = blockIdx.x / max_num_poly_per_anchor;
    int anchor_poly_id = blockIdx.x - anchor_idx * max_num_poly_per_anchor;
    int num_polys_for_this_anchor = per_anchor_poly_idx[anchor_idx+1] - per_anchor_poly_idx[anchor_idx];
    if (anchor_poly_id >= num_polys_for_this_anchor)
	    return;  // no work for this block
    int poly_id = per_anchor_poly_idx[anchor_idx] + anchor_poly_id;

    int tid = threadIdx.x;
    int block_jump = blockDim.x;
    long cnts_offset = poly_id * CNTS_SIZE;
    long k = (poly_rel_idx[poly_id + 1] - poly_rel_idx[poly_id]) / 2;
    
    const double *xy = dense_coordinates + poly_rel_idx[poly_id];
    int *x = x_in + poly_id * BUFFER_SIZE;
    int *y = y_in + poly_id * BUFFER_SIZE;
    int *u = u_in + poly_id * BUFFER_SIZE;
    int *v = v_in + poly_id * BUFFER_SIZE;
    uint *a = a_in + poly_id * BUFFER_SIZE;
    uint *b = b_in + poly_id * BUFFER_SIZE;
    /* upsample and get discrete points densely along entire boundary */
    long j, m = 0;
    double scale = 5;
    __shared__ int shbuf1[BUFFER_SIZE];
    __shared__ int shbuf2[BUFFER_SIZE];
    for(long j = tid; j < BUFFER_SIZE; j += block_jump) {
        shbuf1[j] = 0; 
        shbuf2[j] = 0;
    }
    for(long j = tid; j <= k; j += block_jump) 
        x[j] = j < k ? ((int) (scale * xy[2 * j + 0] + 0.5)) : ((int) (scale * xy[0] + 0.5));
    for(long j = tid; j <= k; j += block_jump) 
        y[j] = j < k ? ((int) (scale * xy[2 * j + 1] + 0.5)) : ((int) (scale * xy[1] + 0.5));        
    __syncthreads();
        
    for(int j = tid; j < k; j += block_jump){
        int xs = x[j], xe = x[j + 1], ys = y[j], ye = y[j + 1], dx, dy, t, d, dist;
        int flip; 
        double s; 
        dx = abs(xe - xs); 
        dy = abs(ys - ye);
        flip = (dx >= dy && xs > xe) || (dx < dy && ys > ye);
        if (flip) {t = xs; xs = xe; xe = t; t = ys; ys = ye; ye = t;}
        s = dx >= dy ? (double) (ye - ys) / dx : (double) (xe - xs) / dy;
        dist = dx >= dy ? dx + 1 : dy + 1;
        shbuf1[j + 1] = dist; 
        shbuf2[j + 1] = dist;
    }
    __syncthreads();
    //block-wide exclusive prefix scan
    int switch_buf = 0;
    for (int offset = 1; offset <= k; offset *= 2){
        switch_buf = 1 - switch_buf;
        if (switch_buf == 0){
            for(int j = tid; j <= k; j += block_jump){
                if (j >= offset) shbuf2[j] = shbuf1[j] + shbuf1[j - offset];
                else shbuf2[j] = shbuf1[j];                
            }
        }
        else if (switch_buf == 1){
            for(int j = tid; j <= k; j += block_jump){
                if (j >= offset) shbuf1[j] = shbuf2[j] + shbuf2[j - offset];
                else shbuf1[j] = shbuf2[j];                
            }                  
        } 
        __syncthreads();
    }
      
    for (int j = tid; j < k; j += block_jump){
        int xs = x[j], xe = x[j + 1], ys = y[j], ye = y[j + 1], dx, dy, t, d, dist;
        int flip; 
        double s; 
        dx = __sad(xe, xs, 0); 
        dy = __sad(ys, ye, 0);
        flip = (dx >= dy && xs > xe) || (dx < dy && ys > ye);
        if (flip) {t = xs; xs = xe; xe = t; t = ys; ys = ye; ye = t;}
        s = dx >= dy ? (double) (ye - ys) / dx : (double) (xe - xs) / dy;
        m = switch_buf == 0 ? shbuf2[j] : shbuf1[j];
        if (dx >= dy) for (d = 0; d <= dx; d++) {
          /*the multiplication statement 's*t' causes nvcc to optimize with flush-to-zero=True for 
          double precision multiply, which we observe produces different results than CPU occasionally. 
          To force flush-to-zero=False, we use __dmul_rn intrinsics function */
          t = flip ? dx - d : d; 
          u[m] = t + xs; 
          v[m] = (int) (ys + __dmul_rn(s, t) + .5); 
          m++; 
        } 
        else for (d = 0; d <= dy; d++) {
          t = flip ? dy - d : d; 
          v[m] = t + ys; 
          u[m] = (int) (xs + __dmul_rn(s, t) + .5); 
          m++;
        }
    }    
    __syncthreads();
    m = switch_buf == 0 ? shbuf2[k] : shbuf1[k];
    int k2 = m;
    __syncthreads();
    double xd, yd;
    if (tid == 0) {
        shbuf1[tid] = 0; 
        shbuf2[tid] = 0;
    }     
    /* get points along y-boundary and downsample */
    for (int j = tid; j < k2; j += block_jump){
        if (j > 0){
            if (u[j] != u[j - 1]){
                xd = (double) (u[j] < u[j-1] ? u[j] : u[j] - 1); 
                xd = (xd + .5) / scale - .5;   
                if (floor(xd) != xd || xd < 0 || xd > w - 1 ) {
                    shbuf1[j] = 0; 
                    shbuf2[j] = 0; 
                    continue;
                }
                yd = (double) (v[j] < v[j - 1] ? v[j] : v[j - 1]); yd = (yd + .5) / scale - .5;
                if (yd < 0) yd = 0; 
                else if (yd > h) yd = h; yd = ceil(yd);                
                shbuf1[j] = 1; 
                shbuf2[j] = 1;               
            } else {
                shbuf1[j] = 0; 
                shbuf2[j] = 0;
            }                    
        }    
    }
    __syncthreads(); 
    //exclusive prefix scan
    switch_buf = 0;
    for (int offset = 1; offset < k2; offset *= 2){
        switch_buf = 1 - switch_buf;
        if (switch_buf == 0){
            for (int j = tid; j < k2; j += block_jump){
                if (j >= offset) shbuf2[j] = shbuf1[j - offset] + shbuf1[j];
                else shbuf2[j] = shbuf1[j];                
            }
        }
        else if (switch_buf == 1){
            for (int j = tid; j < k2; j += block_jump){
                if (j >= offset) shbuf1[j] = shbuf2[j - offset] + shbuf2[j];
                else shbuf1[j] = shbuf2[j];                
            }                  
        } 
        __syncthreads();             
    }
  
    for (int j = tid; j < k2; j += block_jump){
        if (j > 0){
            if(u[j] != u[j - 1]){
                xd = (double) (u[j] < u[j - 1] ? u[j] : u[j] - 1); 
                xd = (xd + .5) / scale - .5;
                if (floor(xd) != xd || xd < 0 || xd > w - 1) {continue;}
                yd = (double) (v[j] < v[j - 1] ? v[j] : v[j - 1]); 
                yd = (yd + .5) / scale - .5;
                if (yd < 0) yd = 0; 
                else if (yd > h) yd = h; yd = ceil(yd);                
                m = switch_buf == 0 ? shbuf2[j - 1]:shbuf1[j - 1];
                x[m] = (int) xd; 
                y[m] = (int) yd; 
                m++;                
            }                   
        }    
    }
    __syncthreads(); 
    
    /* compute rle encoding given y-boundary points */
    m = switch_buf == 0 ? shbuf2[k2 - 1] : shbuf1[k2 - 1]; 
    int k3 = m;
    for (int j = tid; j <= k3; j += block_jump){
       if (j < k3) a[j] = (uint) (x[j] * (int) (h) + y[j]); 
       else a[j] = (uint)(h * w);        
    }    
    k3++;
    __syncthreads();
    
    //run brick sort on a for k3+1 element
    //load k3+1 elements of a into shared memory
    for(long j = tid; j < k3; j += block_jump) shbuf1[j]=a[j];
    __syncthreads();
    uint a_temp;
    for (int r = 0; r <= k3 / 2; r++){
        int evenCas = k3 / 2;
        int oddCas = (k3 - 1) / 2;
        //start with 0, need (k3+1)/2 CAS
        for (int j = tid; j < evenCas; j += block_jump){
            if (shbuf1[2 * j] > shbuf1[2 * j + 1]){
                a_temp = shbuf1[2 * j];
                shbuf1[2 * j]=shbuf1[2 * j + 1];
                shbuf1[2 * j + 1] = a_temp;
            }            
        }
        __syncthreads();
        //start with 1
        for (int j = tid; j < oddCas; j += block_jump){
            if (shbuf1[2 * j + 1] > shbuf1[2 * j + 2]){
                a_temp=shbuf1[2 * j + 1];
                shbuf1[2 * j + 1] = shbuf1[2 * j + 2];
                shbuf1[2 * j + 2]=a_temp;
            }            
        }
        __syncthreads();    
    }
    
    for(long j = tid; j < k3; j += block_jump) {
        if(j>0) shbuf2[j] = shbuf1[j - 1];
        else shbuf2[j] = 0;        
    } 
     __syncthreads();  
    for(int j = tid; j < k3; j += block_jump){
        shbuf1[j] -= shbuf2[j];       
    }      
    __syncthreads();   
    uint *cnts_buf = cnts + cnts_offset;
    if (tid == 0){
        j = m = 0; 
        cnts_buf[m++] = shbuf1[j++];
        while (j < k3) if (shbuf1[j] > 0) cnts_buf[m++] = shbuf1[j++]; else {
            j++; if (j < k3) cnts_buf[m - 1] += shbuf1[j++]; } 
        num_of_cnts[poly_id] = m;               
    }
    __syncthreads();
}

/*
        height, width, id, box_offset = self.after_transforms_img_infos_l[index]
        num_boxes = (self.after_transforms_img_infos_l[index+1][3] - box_offset) // 5
        target = BoxList(
                self.after_transforms_bboxes_and_labels[box_offset:box_offset+num_boxes*4].reshape([-1,4]),
                (width, height,),
                "xyxy"
                )
        labels = self.after_transforms_bboxes_and_labels[box_offset+num_boxes*4:box_offset+num_boxes*5]
        target.add_field('labels', labels)
        mask_offset = self.indexes_l[self.header_size+index]
        num_masks = self.indexes_l[self.header_size+index+1] - mask_offset
        masks = []
        for mask in range(num_masks):
            polygon_offset = self.indexes_l[mask_offset+mask]
            num_polygons = self.indexes_l[mask_offset+mask+1] - polygon_offset
            #print("polygon_offset = %d, num_polygons = %d" % (polygon_offset, num_polygons))
            polygons = []
            for poly in range(num_polygons):
                sample_offset_s = self.indexes_l[polygon_offset+poly]
                sample_offset_e = self.indexes_l[polygon_offset+poly+1]
                #print("sample_offset = %d, num_samples = %d" % (sample_offset_s, sample_offset_e-sample_offset_s))
                polygons.append(self.after_transforms_dense_xy[sample_offset_s:sample_offset_e])
            masks.append(polygons)
        masks = SegmentationMask(masks, (width, height,))
        target.add_field('masks', masks)
        return target, width, height, self.after_transforms_hflip_l[index]
	*/
__global__ void generate_mask_targets_cuda_kernel(
		const int* target_index, // index argument passed by hybrid data loader in get_target call.
		const int* img_infos, // self.after_transforms_img_infos
		const int* indexes, // self.indexes from COCODatasetPyt
		const float *transformed_dense_coordinates, // self.after_transforms_dense_xy
		const float *bboxes_and_labels, // self.after_transforms_bboxes_and_labels
		const int64_t* clamped_idxs, 
		const int max_num_poly_per_anchor, // MUST be global constant, compute in COCODatasetPYT init routine
                const int mask_size,
                byte *mask,
		int *cnts, int *x_in, int *y_in, int *u_in, int *v_in, int *a_in, 
		int *b_in, int *num_of_cnts
		) {
    const int anchor_idx = blockIdx.x / max_num_poly_per_anchor;
    const int anchor_poly_id = blockIdx.x - anchor_idx * max_num_poly_per_anchor;
    const int header_size = indexes[0];
    const int mask_offset = indexes[header_size+*target_index];
    const int mask_id = static_cast<int>(clamped_idxs[anchor_idx]);
    const int polygon_offset = indexes[mask_offset+mask_id];
    const int num_polygons = indexes[mask_offset+mask_id+1] - polygon_offset;
    if (anchor_poly_id >= num_polygons)
	return; // no work for this block

    // determine output polygon
    int poly_id = 0;
    for (int i = 0;  i < anchor_idx;  ++i) {
	int j = static_cast<int>(clamped_idxs[i]);
	poly_id += (indexes[mask_offset+j+1] - indexes[mask_offset+j]); // +num_polygons for mask clamped_idxs[i]
    }
    poly_id += anchor_poly_id;

    int sample_offset = indexes[polygon_offset+anchor_poly_id];
    int k = (indexes[polygon_offset+anchor_poly_id+1] - sample_offset) >> 1;

    int tid = threadIdx.x;
    int block_jump = blockDim.x;
    long cnts_offset = poly_id * CNTS_SIZE;
    const float *xy = transformed_dense_coordinates + sample_offset;
    int *x = x_in + poly_id * BUFFER_SIZE;
    int *y = y_in + poly_id * BUFFER_SIZE;
    int *u = u_in + poly_id * BUFFER_SIZE;
    int *v = v_in + poly_id * BUFFER_SIZE;
    int *a = a_in + poly_id * BUFFER_SIZE;
    int *b = b_in + poly_id * BUFFER_SIZE;
    /* upsample and get discrete points densely along entire boundary */
    long j, m = 0;
    __shared__ uint shbuf1[BUFFER_SIZE];
    __shared__ uint shbuf2[BUFFER_SIZE];
    for(int j = tid; j < BUFFER_SIZE; j += block_jump) {
        shbuf1[j] = 0; 
        shbuf2[j] = 0;
    }
    /* old loop merged with crop_and_scale loop */
    const float scale = 5.0f;
    const int box_offset = img_infos[4*(*target_index)+3] + mask_id*4;
    const int h = img_infos[4*(*target_index)];
    const int w = img_infos[4*(*target_index)+1];
    const float anchor_x0 = bboxes_and_labels[box_offset];
    const float anchor_y0 = bboxes_and_labels[box_offset+1];
    const float anchor_x1 = bboxes_and_labels[box_offset+2];
    const float anchor_y1 = bboxes_and_labels[box_offset+3];
    float ratio_h = ((float)mask_size * scale) / fmaxf(anchor_y1 - anchor_y0, 1.0f);
    float ratio_w = ((float)mask_size * scale) / fmaxf(anchor_x1 - anchor_x0, 1.0f);
    for(int j = tid; j <= k; j += block_jump) {
        x[j] = j < k ? xy[2*j  ] : xy[0];
        y[j] = j < k ? xy[2*j+1] : xy[1];
        x[j] = (int)(scale * ratio_w * (x[j] - anchor_x0) + 0.5f);
        y[j] = (int)(scale * ratio_h * (y[j] - anchor_y0) + 0.5f);
    }
    __syncthreads();
        
    for(int j = tid; j < k; j += block_jump){
        int xs = x[j], xe = x[j + 1], ys = y[j], ye = y[j + 1], dx, dy, t, d, dist;
        int flip; 
        double s; 
        dx = abs(xe - xs); 
        dy = abs(ys - ye);
        flip = (dx >= dy && xs > xe) || (dx < dy && ys > ye);
        if (flip) {t = xs; xs = xe; xe = t; t = ys; ys = ye; ye = t;}
        s = dx >= dy ? (double) (ye - ys) / dx : (double) (xe - xs) / dy;
        dist = dx >= dy ? dx + 1 : dy + 1;
        shbuf1[j + 1] = dist; 
        shbuf2[j + 1] = dist;
    }
    __syncthreads();
    //block-wide exclusive prefix scan
    int switch_buf = 0;
    for (int offset = 1; offset <= k; offset *= 2){
        switch_buf = 1 - switch_buf;
        if (switch_buf == 0){
            for(int j = tid; j <= k; j += block_jump){
                if (j >= offset) shbuf2[j] = shbuf1[j] + shbuf1[j - offset];
                else shbuf2[j] = shbuf1[j];                
            }
        }
        else if (switch_buf == 1){
            for(int j = tid; j <= k; j += block_jump){
                if (j >= offset) shbuf1[j] = shbuf2[j] + shbuf2[j - offset];
                else shbuf1[j] = shbuf2[j];                
            }                  
        } 
        __syncthreads();
    }
      
    for (int j = tid; j < k; j += block_jump){
        int xs = x[j], xe = x[j + 1], ys = y[j], ye = y[j + 1], dx, dy, t, d, dist;
        int flip; 
        double s; 
        dx = __sad(xe, xs, 0); 
        dy = __sad(ys, ye, 0);
        flip = (dx >= dy && xs > xe) || (dx < dy && ys > ye);
        if (flip) {t = xs; xs = xe; xe = t; t = ys; ys = ye; ye = t;}
        s = dx >= dy ? (double) (ye - ys) / dx : (double) (xe - xs) / dy;
        m = switch_buf == 0 ? shbuf2[j] : shbuf1[j];
        if (dx >= dy) for (d = 0; d <= dx; d++) {
          /*the multiplication statement 's*t' causes nvcc to optimize with flush-to-zero=True for 
          double precision multiply, which we observe produces different results than CPU occasionally. 
          To force flush-to-zero=False, we use __dmul_rn intrinsics function */
          t = flip ? dx - d : d; 
          u[m] = t + xs; 
          v[m] = (int) (ys + __dmul_rn(s, t) + .5); 
          m++; 
        } 
        else for (d = 0; d <= dy; d++) {
          t = flip ? dy - d : d; 
          v[m] = t + ys; 
          u[m] = (int) (xs + __dmul_rn(s, t) + .5); 
          m++;
        }
    }    
    __syncthreads();
    m = switch_buf == 0 ? shbuf2[k] : shbuf1[k];
    int k2 = m;
    __syncthreads();
    double xd, yd;
    if (tid == 0) {
        shbuf1[tid] = 0; 
        shbuf2[tid] = 0;
    }     
    /* get points along y-boundary and downsample */
    for (int j = tid; j < k2; j += block_jump){
        if (j > 0){
            if (u[j] != u[j - 1]){
                xd = (double) (u[j] < u[j-1] ? u[j] : u[j] - 1); 
                xd = (xd + .5) / scale - .5;   
                if (floor(xd) != xd || xd < 0 || xd > w - 1 ) {
                    shbuf1[j] = 0; 
                    shbuf2[j] = 0; 
                    continue;
                }
                yd = (double) (v[j] < v[j - 1] ? v[j] : v[j - 1]); yd = (yd + .5) / scale - .5;
                if (yd < 0) yd = 0; 
                else if (yd > h) yd = h; yd = ceil(yd);                
                shbuf1[j] = 1; 
                shbuf2[j] = 1;               
            } else {
                shbuf1[j] = 0; 
                shbuf2[j] = 0;
            }                    
        }    
    }
    __syncthreads(); 
    //exclusive prefix scan
    switch_buf = 0;
    for (int offset = 1; offset < k2; offset *= 2){
        switch_buf = 1 - switch_buf;
        if (switch_buf == 0){
            for (int j = tid; j < k2; j += block_jump){
                if (j >= offset) shbuf2[j] = shbuf1[j - offset] + shbuf1[j];
                else shbuf2[j] = shbuf1[j];                
            }
        }
        else if (switch_buf == 1){
            for (int j = tid; j < k2; j += block_jump){
                if (j >= offset) shbuf1[j] = shbuf2[j - offset] + shbuf2[j];
                else shbuf1[j] = shbuf2[j];                
            }                  
        } 
        __syncthreads();             
    }
  
    for (int j = tid; j < k2; j += block_jump){
        if (j > 0){
            if(u[j] != u[j - 1]){
                xd = (double) (u[j] < u[j - 1] ? u[j] : u[j] - 1); 
                xd = (xd + .5) / scale - .5;
                if (floor(xd) != xd || xd < 0 || xd > w - 1) {continue;}
                yd = (double) (v[j] < v[j - 1] ? v[j] : v[j - 1]); 
                yd = (yd + .5) / scale - .5;
                if (yd < 0) yd = 0; 
                else if (yd > h) yd = h; yd = ceil(yd);                
                m = switch_buf == 0 ? shbuf2[j - 1]:shbuf1[j - 1];
                x[m] = (int) xd; 
                y[m] = (int) yd; 
                m++;                
            }                   
        }    
    }
    __syncthreads(); 
    
    /* compute rle encoding given y-boundary points */
    m = switch_buf == 0 ? shbuf2[k2 - 1] : shbuf1[k2 - 1]; 
    int k3 = m;
    for (int j = tid; j <= k3; j += block_jump){
       if (j < k3) a[j] = (uint) (x[j] * (int) (h) + y[j]); 
       else a[j] = (uint)(h * w);        
    }    
    k3++;
    __syncthreads();
    
    //run brick sort on a for k3+1 element
    //load k3+1 elements of a into shared memory
    for(int j = tid; j < k3; j += block_jump) shbuf1[j]=a[j];
    __syncthreads();
    uint a_temp;
    for (int r = 0; r <= k3 / 2; r++){
        int evenCas = k3 / 2;
        int oddCas = (k3 - 1) / 2;
        //start with 0, need (k3+1)/2 CAS
        for (int j = tid; j < evenCas; j += block_jump){
            if (shbuf1[2 * j] > shbuf1[2 * j + 1]){
                a_temp = shbuf1[2 * j];
                shbuf1[2 * j]=shbuf1[2 * j + 1];
                shbuf1[2 * j + 1] = a_temp;
            }            
        }
        __syncthreads();
        //start with 1
        for (int j = tid; j < oddCas; j += block_jump){
            if (shbuf1[2 * j + 1] > shbuf1[2 * j + 2]){
                a_temp=shbuf1[2 * j + 1];
                shbuf1[2 * j + 1] = shbuf1[2 * j + 2];
                shbuf1[2 * j + 2]=a_temp;
            }            
        }
        __syncthreads();    
    }
    
    for(int j = tid; j < k3; j += block_jump) {
        if(j>0) shbuf2[j] = shbuf1[j - 1];
        else shbuf2[j] = 0;        
    } 
     __syncthreads();  
    for(int j = tid; j < k3; j += block_jump){
        shbuf1[j] -= shbuf2[j];       
    }      
    __syncthreads();   
    int *cnts_buf = cnts + cnts_offset;
    if (tid == 0){
        j = m = 0; 
        cnts_buf[m++] = shbuf1[j++];
        while (j < k3) if (shbuf1[j] > 0) cnts_buf[m++] = shbuf1[j++]; else {
            j++; if (j < k3) cnts_buf[m - 1] += shbuf1[j++]; } 
        num_of_cnts[poly_id] = m;               
    }
    __syncthreads();

    //
    // decode_rle follows
    //

    m = num_of_cnts[poly_id];
    byte *mask_ptr = mask + poly_id * mask_size * mask_size;
    
    //initialize shbuf for scan. first element is 0 (exclusive scan)
    for (int i = tid; i < CNTS_SIZE; i += block_jump){
        shbuf1[i] = (i <= m & i > 0) ? cnts_buf[i - 1]:0;
        shbuf2[i] = (i <= m & i > 0) ? cnts_buf[i - 1]:0;
    }
    __syncthreads();
    
    //double buffering for scan
    switch_buf = 0;
    for (int offset = 1; offset <= m; offset *= 2){
        switch_buf = 1 - switch_buf;
        if(switch_buf == 0){
            for(int j = tid;j <= m;j += block_jump){
                if(j >= offset) shbuf2[j] = shbuf1[j]+shbuf1[j - offset];
                else shbuf2[j] = shbuf1[j];
            }
        }else if (switch_buf == 1){
            for(int j = tid;j <= m;j += block_jump){
                if(j >= offset) shbuf1[j] = shbuf2[j] + shbuf2[j - offset];
                else shbuf1[j] = shbuf2[j];
            }        
        
        }
        __syncthreads();
    }
    uint *scanned_buf = switch_buf == 0 ? shbuf2 : shbuf1;
       
    //find which bin pixel j falls into , which determines the pixel value
    //use binary search
    for(int j = tid; j < mask_size * mask_size; j += block_jump){
        int min_idx = 0;
        int max_idx = m;
        int mid_idx = m / 2;        
        while(max_idx > min_idx){
            if(j > scanned_buf[mid_idx]) {
                min_idx = mid_idx+1; 
                mid_idx = (min_idx + max_idx) / 2;
            }
            else if (j < scanned_buf[mid_idx]) {
                max_idx = mid_idx; 
                mid_idx = (min_idx + max_idx) / 2;
            }
            else {
                mid_idx++; 
                break;
            }    
        }         
        int k = mid_idx;
        byte pixel = k % 2 == 0 ? 1 : 0;
        mask_ptr[j] = pixel; 
    }
    __syncthreads(); // not needed unless more code follows
}

//merging masks happens on mask format, not RLE format.     
__global__ void global_transforms_merge_masks_cuda_kernel(
	byte *masks_in, 
	float *masks_out, 
	const int mask_size, 
	const int* target_index,
	const int* indexes,
	const int64_t* clamped_idxs
	)
{
    const int anchor_idx = blockIdx.x;
    const int tid = threadIdx.x;
    const int jump_block = blockDim.x;
    const int index = *target_index;
    const int header_size = indexes[0];
    const int mask_offset = indexes[header_size+index];
    const int num_of_masks_to_merge = indexes[header_size+index+1] - mask_offset;
    
    // determine output polygon
    int mask_start_idx = 0;
    for (int i = 0;  i < anchor_idx;  ++i) {
	int j = static_cast<int>(clamped_idxs[i]);
	mask_start_idx += (indexes[mask_offset+j+1] - indexes[mask_offset+j]); // +num_polygons for mask clamped_idxs[i]
    }
    
    for(int j = tid; j < mask_size * mask_size; j += jump_block){
        int transposed_pixel = (j % mask_size) * mask_size + j / mask_size;
        byte pixel = 0;
        for(int k = 0; k < num_of_masks_to_merge; k++){
            if (masks_in[(mask_start_idx + k) * mask_size * mask_size + j] == 1) pixel = 1;
            if (pixel == 1) break;
        }
        masks_out[anchor_idx * mask_size * mask_size + transposed_pixel] = (float) pixel;       
    }
}

at::Tensor global_generate_mask_targets_cuda(
	at::Tensor target_index,
	at::Tensor transformed_img_infos,
	at::Tensor indexes,
	at::Tensor transformed_dense_coordinates,
	at::Tensor transformed_bboxes_and_labels,
	at::Tensor clamped_idxs,
	const int max_num_poly_per_anchor,
	const int mask_size
	)
{
    const int M = mask_size;
    assert (M < 32); 
    //if M >=32, shared memory buffer size may not be
    //sufficient. Need to fix this by blocking    

    const int num_of_anchors = clamped_idxs.size(0);
    const int max_num_of_poly = num_of_anchors * max_num_poly_per_anchor;
    at::Tensor d_x_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_y_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_u_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_v_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_a_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));//used with uint* pointer
    at::Tensor d_b_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt)); //used with uint* pointer
    at::Tensor d_mask_t = torch::empty({M * M * max_num_of_poly}, torch::CUDA(at::kByte));
    auto result =  torch::empty({num_of_anchors, M, M}, torch::CUDA(at::kFloat));
    at::Tensor d_num_of_counts_t = torch::empty({max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_cnts_t = torch::empty({CNTS_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));

    auto stream = at::cuda::getCurrentCUDAStream();

    generate_mask_targets_cuda_kernel<<<max_num_of_poly, 1024, 0, stream.stream()>>>(
	    target_index.data_ptr<int>(),
	    transformed_img_infos.data_ptr<int>(),
	    indexes.data_ptr<int>(),
	    transformed_dense_coordinates.data_ptr<float>(),
	    transformed_bboxes_and_labels.data_ptr<float>(),
	    clamped_idxs.data_ptr<int64_t>(),
	    max_num_poly_per_anchor,
	    mask_size,
	    d_mask_t.data_ptr<byte>(),
	    d_cnts_t.data_ptr<int>(),
	    d_x_t.data_ptr<int>(),
	    d_y_t.data_ptr<int>(),
	    d_u_t.data_ptr<int>(),
	    d_v_t.data_ptr<int>(),
	    d_a_t.data_ptr<int>(),
	    d_b_t.data_ptr<int>(),
	    d_num_of_counts_t.data_ptr<int>()
	    );

    // cannot be merged with above kernel because all blocks must finish before this kernel can launch.
    global_transforms_merge_masks_cuda_kernel<<<num_of_anchors, 256, 0, stream.stream()>>>(
	    d_mask_t.data<byte>(), 
	    result.data_ptr<float>(),
            M, 
	    target_index.data_ptr<int>(),
	    indexes.data_ptr<int>(),
	    clamped_idxs.data_ptr<int64_t>());

    return result;
}

// TODO: Add launch codes. Need to determine max num polys for all samples.
// Can do this in COCODatasetPYT init routine.

at::Tensor generate_mask_targets_cuda(at::Tensor dense_vector, const std::vector<std::vector<at::Tensor>> polygons, 
                                      const at::Tensor anchors, const int mask_size){    
    const int M = mask_size;
    assert (M < 32); 
    //if M >=32, shared memory buffer size may not be
    //sufficient. Need to fix this by blocking    
    float *d_anchor_data = anchors.data_ptr<float>();
    int num_of_anchors = anchors.size(0);  
    auto options = torch::dtype(torch::kInt).device(torch::kCPU).pinned_memory(true);
    auto per_anchor_poly_idx = at::empty({num_of_anchors + 1}, options);
    int nn = 0, max_num_poly_per_anchor = 0;
    // NB!
    // This loop introduces a race condition if this function is called more than once per step.
    // Note that this function is called once per image. The code runs correctly because the original
    // code has a GPU-CPU sync per image, but will not work if this sync is removed. That's why
    // there is a syncfree_ version of this function.
    for (int i = 0; i < num_of_anchors; i++){
            *(per_anchor_poly_idx.data_ptr<int>() + i) = nn;
	    int this_num_poly = polygons[i].size();
	    if (this_num_poly > max_num_poly_per_anchor) max_num_poly_per_anchor = this_num_poly;
            nn += this_num_poly;
    }
    *(per_anchor_poly_idx.data_ptr<int>() + num_of_anchors) = nn;
    int max_num_of_poly = num_of_anchors * max_num_poly_per_anchor;

    auto poly_rel_idx = at::empty({max_num_of_poly + 1}, options);
    double *dense_poly_data = dense_vector.data_ptr<double>();
    int start_idx = 0;
    int poly_count = 0;
  
    for(int i = 0; i < polygons.size(); i++){
  	    for(int j=0; j < polygons[i].size(); j++) {
                    *(poly_rel_idx.data_ptr<int>() + poly_count) = start_idx;
  		    start_idx += polygons[i][j].size(0);
  		    poly_count++;
  	    }
    }    
    *(poly_rel_idx.data_ptr<int>() + poly_count) = start_idx;

    at::Tensor d_x_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_y_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_u_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_v_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_a_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));//used with uint* pointer
    at::Tensor d_b_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt)); //used with uint* pointer
    at::Tensor d_mask_t = torch::empty({M * M * max_num_of_poly}, torch::CUDA(at::kByte));
    auto result =  torch::empty({num_of_anchors, M, M}, torch::CUDA(at::kFloat));
    at::Tensor d_num_of_counts_t = torch::empty({max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_cnts_t = torch::empty({CNTS_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    auto d_dense_vector = dense_vector.cuda();
    auto d_per_anchor_poly_idx = per_anchor_poly_idx.to(torch::kCUDA, true);
    auto d_poly_rel_idx = poly_rel_idx.to(torch::kCUDA, true);
    auto stream = at::cuda::getCurrentCUDAStream();
    
    crop_and_scale_cuda_kernel<<<max_num_of_poly, 256, 0, stream.stream()>>>(d_dense_vector.data_ptr<double>(),
                                                                      d_per_anchor_poly_idx.data_ptr<int>(),
                                                                      d_poly_rel_idx.data_ptr<int>(),
								      max_num_poly_per_anchor,
                                                                      (float4*) d_anchor_data,
                                                                      M);

    //TODO: larger threads-per-block might be better here, because each CTA uses 32 KB of shmem,
    //and occupancy is likely shmem capacity bound                                                                                
    rle_fr_poly_cuda_kernel<<<max_num_of_poly, 1024, 0, stream.stream()>>>(d_dense_vector.data_ptr<double>(),
                                                                   d_poly_rel_idx.data_ptr<int>(),
                                                                   M, M,
                                                                   (uint*) d_cnts_t.data_ptr<int>(),
                                                                   d_x_t.data_ptr<int>(),
                                                                   d_y_t.data_ptr<int>(),
                                                                   d_u_t.data_ptr<int>(),
                                                                   d_v_t.data_ptr<int>(),
                                                                   (uint*) d_a_t.data_ptr<int>(),
                                                                   (uint*) d_b_t.data_ptr<int>(),
                                                                   d_num_of_counts_t.data_ptr<int>(),
								   d_per_anchor_poly_idx.data_ptr<int>(),
								   max_num_poly_per_anchor);
                                                                 
    decode_rle_cuda_kernel<<<max_num_of_poly, 256, 0, stream.stream()>>>(d_num_of_counts_t.data_ptr<int>(),
                                                                 (uint*) d_cnts_t.data_ptr<int>(),
                                                                 M, M,
                                                                 d_mask_t.data_ptr<byte>(),
								 d_per_anchor_poly_idx.data_ptr<int>(),
								 max_num_poly_per_anchor);
                                                                 
    merge_masks_cuda_kernel<<<num_of_anchors, 256, 0, stream.stream()>>>(d_mask_t.data<byte>(), result.data_ptr<float>(),
                                                                      M, d_per_anchor_poly_idx.data_ptr<int>());
    return result;
}

at::Tensor syncfree_generate_mask_targets_cuda(at::Tensor clamped_idxs, const std::vector<std::vector<at::Tensor>> polygons, const at::Tensor anchors, const int mask_size)
{
    const int M = mask_size;
    assert (M < 32);
    //if M >=32, shared memory buffer size may not be
    //sufficient. Need to fix this by blocking

    // dimensions
    int num_masks = polygons.size();
    int num_of_anchors = anchors.size(0);
    int max_num_poly_per_anchor = 0, max_samples_per_anchor = 0, segmask_num_poly = 0;
    for (int i = 0;  i < polygons.size();  ++i) {
        int num_of_poly = polygons[i].size();
        segmask_num_poly += num_of_poly;
        max_num_poly_per_anchor = num_of_poly > max_num_poly_per_anchor ? num_of_poly : max_num_poly_per_anchor;
        int num_samples_this_anchor = 0;
        for (int j = 0;  j < polygons[i].size();  ++j) {
            int num_samples_this_poly = polygons[i][j].size(0);
            num_samples_this_anchor += num_samples_this_poly;
        }
        max_samples_per_anchor = num_samples_this_anchor > max_samples_per_anchor ? num_samples_this_anchor : max_samples_per_anchor;
    }
    int max_num_of_poly = num_of_anchors * max_num_poly_per_anchor;
    assert(num_of_anchors == clamped_idxs.numel());
    if (num_masks <= 0 || num_of_anchors <= 0 || max_num_poly_per_anchor <= 0 || max_samples_per_anchor <= 0 || segmask_num_poly <= 0) {
        printf("num_masks=%d, num_of_anchors=%d, max_num_poly_per_anchor=%d, max_samples_per_anchor=%d, segmask_num_poly=%d\n",num_masks,num_of_anchors,max_num_poly_per_anchor,max_samples_per_anchor,segmask_num_poly);
    }

    // create input tensors for ROI kernels.
    std::vector<at::Tensor> poly_vec;
    std::vector<int> polygons_per_segmask(num_masks, 0);
    std::vector<int> samples_per_segmask_polygon(segmask_num_poly+1, 0);
    std::vector<int> samples_per_segmask(num_masks, 0);
    for (int i = 0, k = 1, acc = 0;  i < num_masks;  ++i) {
	polygons_per_segmask[i] = polygons[i].size();
        int num_samples_this_segmask = 0;
	for (int j = 0;  j < polygons[i].size();  ++j) {
	    poly_vec.push_back(polygons[i][j]);
	    int num_samples_this_polygon = polygons[i][j].size(0);
	    num_samples_this_segmask += num_samples_this_polygon;
	    acc += num_samples_this_polygon;
	    samples_per_segmask_polygon[k++] = acc;
	}
	samples_per_segmask[i] = num_samples_this_segmask;
    }

    auto per_segmask_dense_poly_data = at::cat(poly_vec, 0);

    auto options = torch::dtype(torch::kInt).device(torch::kCPU).pinned_memory(true);
    auto per_segmask_poly_idx = at::tensor(polygons_per_segmask, options).to(torch::kCUDA, true);
    auto per_segmask_poly_rel_idx = at::tensor(samples_per_segmask_polygon, options).to(torch::kCUDA, true);
    auto per_segmask_num_samples = at::tensor(samples_per_segmask, options).to(torch::kCUDA, true);

    auto per_anchor_poly_idx = per_segmask_poly_idx.index_select(0, clamped_idxs);
    auto per_anchor_first_poly_rel_idx = per_segmask_num_samples.index_select(0, clamped_idxs);

    per_anchor_poly_idx = at::cumsum(per_anchor_poly_idx, 0, torch::kInt);
    per_anchor_poly_idx = at::cat({at::zeros({1},torch::CUDA(at::kInt)), per_anchor_poly_idx}, 0);

    per_segmask_poly_idx = at::cumsum(per_segmask_poly_idx, 0, torch::kInt);
    per_segmask_poly_idx = at::cat({at::zeros({1},torch::CUDA(at::kInt)), per_segmask_poly_idx}, 0);

    per_anchor_first_poly_rel_idx = at::cumsum(per_anchor_first_poly_rel_idx, 0, torch::kInt);
    per_anchor_first_poly_rel_idx = at::cat({at::zeros({1},torch::CUDA(at::kInt)), per_anchor_first_poly_rel_idx}, 0);

    // populate input tensors.
    auto stream = at::cuda::getCurrentCUDAStream();
    int num_blocks = (num_of_anchors + 255) / 256;
    auto per_anchor_poly_rel_idx = at::empty({max_num_of_poly + 1}, torch::CUDA(at::kInt));
    create_poly_rel_idx_kernel<<<num_blocks, 256, 0, stream.stream()>>>(
	    per_segmask_poly_idx.data_ptr<int>(),
	    per_segmask_poly_rel_idx.data_ptr<int>(),
	    clamped_idxs.data_ptr<int64_t>(),
	    num_of_anchors,
	    per_anchor_poly_idx.data_ptr<int>(),
	    per_anchor_first_poly_rel_idx.data_ptr<int>(),
	    per_anchor_poly_rel_idx.data_ptr<int>());
    auto per_anchor_dense_poly_data = at::empty({num_of_anchors * max_samples_per_anchor}, torch::CUDA(at::kDouble));
    AT_DISPATCH_FLOATING_TYPES_AND_HALF(per_segmask_dense_poly_data.scalar_type(), "create_dense_poly_data_kernel", [&](){
            create_dense_poly_data_kernel<<<num_of_anchors, 256, 0, stream.stream()>>>(
	        per_segmask_poly_idx.data_ptr<int>(),
	        per_segmask_poly_rel_idx.data_ptr<int>(),
	        clamped_idxs.data_ptr<int64_t>(),
	        per_anchor_poly_idx.data_ptr<int>(),
	        per_anchor_poly_rel_idx.data_ptr<int>(),
                per_segmask_dense_poly_data.data_ptr<scalar_t>(),
                per_anchor_dense_poly_data.data_ptr<double>());
	    });

    // call kernels
    at::Tensor d_x_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_y_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_u_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_v_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_a_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));//used with uint* pointer
    at::Tensor d_b_t = torch::empty({BUFFER_SIZE * max_num_of_poly}, torch::CUDA(at::kInt)); //used with uint* pointer
    at::Tensor d_mask_t = torch::empty({M * M * max_num_of_poly}, torch::CUDA(at::kByte));
    auto result =  torch::empty({num_of_anchors, M, M}, torch::CUDA(at::kFloat));
    at::Tensor d_num_of_counts_t = torch::empty({max_num_of_poly}, torch::CUDA(at::kInt));
    at::Tensor d_cnts_t = torch::empty({CNTS_SIZE * max_num_of_poly}, torch::CUDA(at::kInt));

    float *d_anchor_data = anchors.data_ptr<float>();
    crop_and_scale_cuda_kernel<<<max_num_of_poly, 256, 0, stream.stream()>>>(per_anchor_dense_poly_data.data_ptr<double>(),
                                                                      per_anchor_poly_idx.data_ptr<int>(),
                                                                      per_anchor_poly_rel_idx.data_ptr<int>(),
								      max_num_poly_per_anchor,
                                                                      (float4*) anchors.data_ptr<float>(),
                                                                      M);

    //TODO: larger threads-per-block might be better here, because each CTA uses 32 KB of shmem,
    //and occupancy is likely shmem capacity bound
    rle_fr_poly_cuda_kernel<<<max_num_of_poly, 1024, 0, stream.stream()>>>(per_anchor_dense_poly_data.data_ptr<double>(),
                                                                   per_anchor_poly_rel_idx.data_ptr<int>(),
                                                                   M, M,
                                                                   (uint*) d_cnts_t.data_ptr<int>(),
                                                                   d_x_t.data_ptr<int>(),
                                                                   d_y_t.data_ptr<int>(),
                                                                   d_u_t.data_ptr<int>(),
                                                                   d_v_t.data_ptr<int>(),
                                                                   (uint*) d_a_t.data_ptr<int>(),
                                                                   (uint*) d_b_t.data_ptr<int>(),
                                                                   d_num_of_counts_t.data_ptr<int>(),
								   per_anchor_poly_idx.data_ptr<int>(),
								   max_num_poly_per_anchor);

    decode_rle_cuda_kernel<<<max_num_of_poly, 256, 0, stream.stream()>>>(d_num_of_counts_t.data_ptr<int>(),
                                                                 (uint*) d_cnts_t.data_ptr<int>(),
                                                                 M, M,
                                                                 d_mask_t.data_ptr<byte>(),
								 per_anchor_poly_idx.data_ptr<int>(),
								 max_num_poly_per_anchor);

    merge_masks_cuda_kernel<<<num_of_anchors, 256, 0, stream.stream()>>>(d_mask_t.data<byte>(), result.data_ptr<float>(),
                                                                      M, per_anchor_poly_idx.data_ptr<int>());
    return result;
}
