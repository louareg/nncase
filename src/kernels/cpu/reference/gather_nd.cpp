/* Copyright 2019-2021 Canaan Inc.
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
#include <nncase/kernels/cpu/reference/tensor_compute.h>
#include <nncase/kernels/kernel_utils.h>
#include <nncase/runtime/runtime_op_utility.h>

using namespace nncase;
using namespace nncase::runtime;
using namespace nncase::kernels;
using namespace nncase::kernels::cpu;
using namespace nncase::kernels::cpu::reference;

namespace
{
template <class T>
result<void> gather_nd_impl(const T *input, T *output, const runtime_shape_t &in_shape, const runtime_shape_t &out_shape,
    const runtime_shape_t &in_strides, const runtime_shape_t &out_strides, const int32_t *indices, const runtime_shape_t &indices_shape, size_t batch_dims,
    NNCASE_UNUSED kernel_context &context) noexcept
{
    return apply(out_shape, [&](const runtime_shape_t &out_index) -> result<void> {
        size_t last_indices_index = indices_shape.size() - 1;
        size_t i_index = 0, o_index = 0;
        runtime_shape_t in_index(in_shape.size());
        runtime_shape_t indices_index(indices_shape.size());
        for (; i_index < batch_dims; i_index++, o_index++)
        {
            // 0-batch_dims
            indices_index[i_index] = out_index[o_index];
            in_index[i_index] = out_index[o_index];
        }
        for (; o_index < last_indices_index; ++o_index)
        {
            // batch_dims - last_indices
            indices_index[o_index] = out_index[o_index];
        }

        auto indices_begin = indices + offset(get_default_strides(indices_shape), indices_index);
        for (size_t i = 0; i < indices_shape[last_indices_index]; ++i_index, ++i)
        {
            // batch_dims-indices_last
            in_index[i_index] = indices_begin[i];
        }

        // out last value is used in block
        // in_shape == [s1 ...] and indices shape is [in_shape.size()], output size will be 1
        // if not judge i_index, i_index will bigger than in_index.size()
        for (; o_index < out_index.size() && i_index < in_index.size(); ++o_index, ++i_index)
        {
            in_index[i_index] = out_index[o_index];
        }
        output[offset(out_strides, out_index)] = input[offset(in_strides, in_index)];
        return ok();
    });
}
}

#define GATHER_ND_IMPL(size, type) \
    case size:                     \
        return gather_nd_impl(reinterpret_cast<const type *>(input), reinterpret_cast<type *>(output), in_shape, out_shape, in_strides, out_strides, indices, indices_shape, batch_dims, context);

result<void> reference::gather_nd(datatype_t type, const gsl::byte *input, gsl::byte *output, const runtime_shape_t &in_shape, const runtime_shape_t &out_shape, 
    const runtime_shape_t &in_strides, const runtime_shape_t &out_strides, const int32_t *indices, const runtime_shape_t &indices_shape, size_t batch_dims, kernel_context &context) noexcept
{
    TYPE_IMPL_SELECT(type, GATHER_ND_IMPL);
}
