# Copyright (c) 2017-2018, NVIDIA CORPORATION. All rights reserved.
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

#pylint: disable=no-member
import sys
import copy
from itertools import count
from nvidia.dali import backend as b
from nvidia.dali.tensor import TensorReference
from future.utils import with_metaclass

class _OpCounter(object):
    #pylint: disable=too-few-public-methods
    _op_count = count(0)
    def __init__(self):
        self._id = next(self._op_count)

    @property
    def id(self):
        return self._id

class _OperatorInstance(object):
    def __init__(self, inputs, op, **kwargs):
        self._counter = _OpCounter()
        self._inputs = inputs
        self._outputs = []
        self._op = op
        self._spec = op.spec.copy()
        if "name" in kwargs.keys():
            self._name = kwargs["name"]
        else:
            self._name = '__' + type(op).__name__ + "_" + str(self._counter.id)
        # Add inputs
        if inputs:
            if isinstance(inputs[0], TensorReference):
                for inp in inputs:
                    if not isinstance(inp, TensorReference):
                        raise TypeError(
                            """Expected inputs of type
                            TensorReference. Received
                            input type {}"""
                            .format(type(inp).__name__))
                    self._spec.AddInput(inp.name, inp.device)
            elif isinstance(inputs[0], list):
                length = len(inputs[0])
                for i in range(length):
                    for inp in inputs:
                        if not isinstance(inp, list):
                            raise TypeError(
                                """Expected inputs of type list of
                                TensorReference. Received
                                input type {}"""
                                .format(type(inp).__name__))
                        if len(inp) != length:
                            raise RuntimeError(
                                    """Expected input lists
                                    to have the same length
                                    ({}). Received list of
                                    length {}"""
                                    .format(length, len(inp)))
                        if not isinstance(inp[i], TensorReference):
                            raise TypeError(
                                """Expected inputs of type
                                TensorReference. Received
                                input type {}"""
                                .format(type(inp[i]).__name__))
                        self._spec.AddInput(inp[i].name, inp[i].device)
                self._spec.AddArg("num_input_sets", length)
            else:
                raise TypeError(
                    """Expected inputs of type TensorReference or list of
                    TensorReference. Received input type {}"""
                    .format(type(inputs[0]).__name__))
        # Argument inputs
        for k in kwargs.keys():
            if k not in ["name"]:
                if not isinstance(kwargs[k], TensorReference):
                    raise TypeError(
                            """Expected inputs of type
                            TensorReference. Received
                            input type {}"""
                            .format(type(inp).__name__))
                self._spec.AddArgumentInput(k, kwargs[k].name)
                self._inputs = list(self._inputs) + [kwargs[k]]

    def check_args(self):
        self._op.schema.CheckArgs(self._spec)

    def generate_outputs(self):
        # Add outputs
        if self._op.device == "gpu" or self._op.device == "mixed":
            output_device = "gpu"
        else:
            output_device = "cpu"

        num_output = self._op.schema.CalculateOutputs(self._spec)

        for i in range(num_output):
            t_name = type(self._op).__name__ + "_id_" + str(self.id) + "_output_" + str(i)
            t = TensorReference(t_name, output_device, self)
            self._spec.AddOutput(t.name, t.device)
            self.append_output(t)

    @property
    def id(self):
        return self._counter.id

    @property
    def inputs(self):
        return self._inputs

    @property
    def outputs(self):
        return self._outputs

    @property
    def spec(self):
        return self._spec

    @property
    def name(self):
        return self._name

    def append_output(self, output):
        self._outputs.append(output)

class _DaliOperatorMeta(type):
    @property
    def __doc__(self):
        return self._docstring()

def python_op_factory(name, op_device = "cpu"):
    class Operator(with_metaclass(_DaliOperatorMeta, object)):
        def __init__(self, **kwargs):
            self._spec = b.OpSpec(type(self).__name__)
            self._schema = b.GetSchema(type(self).__name__)

            # Get the device argument. We will need this to determine
            # the device that our outputs will be stored on
            if "device" in kwargs.keys():
                self._device = kwargs["device"]
            else:
                self._spec.AddArg("device", op_device)
                self._device = op_device

            # Store the specified arguments
            for key, value in kwargs.items():
                if isinstance(value, list):
                    if not value:
                        raise RuntimeError("List arguments need to have at least 1 element.")
                self._spec.AddArg(key, value)

        @classmethod
        def _docstring(cls):
            schema = b.GetSchema(cls.__name__)
            return schema.Dox()

        @property
        def spec(self):
            return self._spec

        @property
        def schema(self):
            return self._schema

        @property
        def device(self):
            return self._device

        def __call__(self, *inputs, **kwargs):
            if (len(inputs) > self._schema.MaxNumInput() or
                    len(inputs) < self._schema.MinNumInput()):
                raise ValueError(
                    """Operator {} expects [{},
                    {}] inputs, but received {}"""
                    .format(type(self).__name__,
                            self._schema.MinNumInput(),
                            self._schema.MaxNumInput(),
                            len(inputs)))

            op_instance = _OperatorInstance(inputs, self, **kwargs)
            op_instance.generate_outputs()

            if len(op_instance.outputs) == 1:
                return op_instance.outputs[0]
            return op_instance.outputs

    Operator.__name__ = str(name)
    return Operator

_cpugpu_ops = (set(b.RegisteredCPUOps())
            .union(set(b.RegisteredGPUOps()))
            .union(set(b.RegisteredMixedOps())))
_support_ops = set(b.RegisteredSupportOps())
for op_name in _cpugpu_ops:
    setattr(sys.modules[__name__], op_name,
            python_op_factory(op_name, op_device = "cpu"))
# add support ops
for op_name in _support_ops:
    setattr(sys.modules[__name__], op_name,
            python_op_factory(op_name, op_device = "support"))

# custom wrappers around ops

class TFRecordReader(with_metaclass(_DaliOperatorMeta, object)):
    def __init__(self, path, index_path, features, **kwargs):
        if isinstance(path, list):
            self._path = path
        else:
            self._path = [path]
        if isinstance(index_path, list):
            self._index_path = index_path
        else:
            self._index_path = [index_path]
        self._schema = b.GetSchema("_TFRecordReader")
        self._spec = b.OpSpec("_TFRecordReader")
        self._device = "cpu"

        self._spec.AddArg("path", self._path)
        self._spec.AddArg("index_path", self._index_path)

        for key, value in kwargs.items():
            self._spec.AddArg(key, value)

        self._features = features

    @classmethod
    def _docstring(cls):
        schema = b.GetSchema("TFRecordReader")
        return schema.Dox()

    @property
    def spec(self):
        return self._spec

    @property
    def schema(self):
        return self._schema

    @property
    def device(self):
        return self._device

    def __call__(self, *inputs, **kwargs):
        if (len(inputs) > self._schema.MaxNumInput() or
                len(inputs) < self._schema.MinNumInput()):
            raise ValueError(
                """Operator {} expects [{},
                {}] inputs, but received {}"""
                .format(type(self).__name__,
                        self._schema.MinNumInput(),
                        self._schema.MaxNumInput(),
                        len(inputs)))

        op_instance = _OperatorInstance(inputs, self, **kwargs)
        outputs = {}
        feature_names = []
        features = []
        for i, (feature_name, feature) in enumerate(self._features.items()):
            t_name = "_TFRecordReader" + "_id_" + str(op_instance.id) + "_output_" + str(i)
            t = TensorReference(t_name, self._device, op_instance)
            op_instance.spec.AddOutput(t.name, t.device)
            op_instance.append_output(t)
            outputs[feature_name] = t
            feature_names.append(feature_name)
            features.append(feature)

        op_instance.spec.AddArg("feature_names", feature_names)
        op_instance.spec.AddArg("features", features)
        return outputs