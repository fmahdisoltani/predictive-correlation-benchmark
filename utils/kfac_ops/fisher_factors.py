from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from utils.kfac_ops import utils
from tensorflow.python.framework import ops as tf_ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import linalg_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import special_math_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import variables
from tensorflow.python.training import moving_averages

import abc
import contextlib
import numpy as np
import six

# Whether to initialize covariance estimators at a zero matrix (or the identity
# matrix).
INIT_COVARIANCES_AT_ZERO = False

# Whether to zero-debias the moving averages.
ZERO_DEBIAS = False

# When the number of inverses requested from a FisherFactor exceeds this value,
# the inverses are computed using an eigenvalue decomposition.
EIGENVALUE_DECOMPOSITION_THRESHOLD = 2

# Numerical eigenvalues computed from covariance matrix estimates are clipped to
# be at least as large as this value before they are used to compute inverses or
# matrix powers. Must be nonnegative.
EIGENVALUE_CLIPPING_THRESHOLD = 0.0


@contextlib.contextmanager
def _maybe_colocate_with(op, colocate_cov_ops_with_inputs):
    """Context to colocate with `op` if `colocate_cov_ops_with_inputs`."""
    if colocate_cov_ops_with_inputs:
        if isinstance(op, (list, tuple)):
            with tf_ops.colocate_with(op[0]):
                yield
        else:
            with tf_ops.colocate_with(op):
                yield
    else:
        yield


def set_global_constants(init_covariances_at_zero=None,
                         zero_debias=None,
                         eigenvalue_decomposition_threshold=None,
                         eigenvalue_clipping_threshold=None):
    """Sets various global constants used by the classes in this module."""
    global INIT_COVARIANCES_AT_ZERO
    global ZERO_DEBIAS
    global EIGENVALUE_DECOMPOSITION_THRESHOLD
    global EIGENVALUE_CLIPPING_THRESHOLD

    if init_covariances_at_zero is not None:
        INIT_COVARIANCES_AT_ZERO = init_covariances_at_zero
    if zero_debias is not None:
        ZERO_DEBIAS = zero_debias
    if eigenvalue_decomposition_threshold is not None:
        EIGENVALUE_DECOMPOSITION_THRESHOLD = eigenvalue_decomposition_threshold
    if eigenvalue_clipping_threshold is not None:
        EIGENVALUE_CLIPPING_THRESHOLD = eigenvalue_clipping_threshold


def inverse_initializer(shape, dtype, partition_info=None):  # pylint: disable=unused-argument
    return array_ops.diag(array_ops.ones(shape[0], dtype))


def covariance_initializer(shape, dtype, partition_info=None):  # pylint: disable=unused-argument
    if INIT_COVARIANCES_AT_ZERO:
        return array_ops.diag(array_ops.zeros(shape[0], dtype))
    return array_ops.diag(array_ops.ones(shape[0], dtype))

def diagonal_covariance_initializer(shape, dtype, partition_info):  # pylint: disable=unused-argument
    if INIT_COVARIANCES_AT_ZERO:
        return array_ops.zeros(shape, dtype)
    return array_ops.ones(shape, dtype)


def _compute_cov(tensor, tensor_right=None, normalizer=None):
    """Compute the empirical second moment of the rows of a 2D Tensor.
    This function is meant to be applied to random matrices for which the true row
    mean is zero, so that the true second moment equals the true covariance.
    Args:
      tensor: A 2D Tensor.
      tensor_right: An optional 2D Tensor. If provided, this function computes
        the matrix product tensor^T * tensor_right instead of tensor^T * tensor.
      normalizer: optional scalar for the estimator (by default, the normalizer is
          the number of rows of tensor).
    Returns:
      A square 2D Tensor with as many rows/cols as the number of input columns.
    """
    if normalizer is None:
        normalizer = array_ops.shape(tensor)[0]
    if tensor_right is None:
        cov = (
            math_ops.matmul(tensor, tensor, transpose_a=True) / math_ops.cast(
                normalizer, tensor.dtype))
        return (cov + array_ops.transpose(cov)) / math_ops.cast(2.0, cov.dtype)
    else:
        return (math_ops.matmul(tensor, tensor_right, transpose_a=True) /
                math_ops.cast(normalizer, tensor.dtype))


def _append_homog(tensor):
    """Appends a homogeneous coordinate to the last dimension of a Tensor.
    Args:
      tensor: A Tensor.
    Returns:
      A Tensor identical to the input but one larger in the last dimension.  The
      new entries are filled with ones.
    """
    rank = len(tensor.shape.as_list())
    shape = array_ops.concat([array_ops.shape(tensor)[:-1], [1]], axis=0)
    ones = array_ops.ones(shape, dtype=tensor.dtype)
    return array_ops.concat([tensor, ones], axis=rank - 1)


def scope_string_from_params(params):
    """Builds a variable scope string name from the given parameters.
    Supported parameters are:
      * tensors
      * booleans
      * ints
      * strings
      * depth-1 tuples/lists of ints
      * any depth tuples/lists of tensors
    Other parameter types will throw an error.
    Args:
      params: A parameter or list of parameters.
    Returns:
      A string to use for the variable scope.
    Raises:
      ValueError: if params includes an unsupported type.
    """
    params = params if isinstance(params, (tuple, list)) else (params,)

    name_parts = []
    for param in params:
        if isinstance(param, (tuple, list)):
            if all([isinstance(p, int) for p in param]):
                name_parts.append("-".join([str(p) for p in param]))
            else:
                name_parts.append(scope_string_from_name(param))
        elif isinstance(param, (str, int, bool)):
            name_parts.append(str(param))
        elif isinstance(param, (tf_ops.Tensor, variables.Variable)):
            name_parts.append(scope_string_from_name(param))
        else:
            raise ValueError("Encountered an unsupported param type {}".format(
                type(param)))
    return "_".join(name_parts)


def scope_string_from_name(tensor):
    if isinstance(tensor, (tuple, list)):
        return "__".join([scope_string_from_name(t) for t in tensor])
    # "gradients/add_4_grad/Reshape:0" -> "gradients_add_4_grad_Reshape"
    return tensor.name.split(":")[0].replace("/", "_")


def scalar_or_tensor_to_string(val):
    return repr(val) if np.isscalar(val) else scope_string_from_name(val)


@six.add_metaclass(abc.ABCMeta)
class FisherFactor(object):
    """Base class for objects modeling factors of approximate Fisher blocks.
       Note that for blocks that aren't based on approximations, a 'factor' can
       be the entire block itself, as is the case for the diagonal and full
       representations.
       Subclasses must implement the _compute_new_cov method, and the _var_scope
       and _cov_shape properties.
    """

    def __init__(self):
        self.instantiate_covariance()

    @abc.abstractproperty
    def _var_scope(self):
        pass

    @abc.abstractproperty
    def _cov_shape(self):
        """The shape of the cov matrix."""
        pass

    @abc.abstractproperty
    def _num_sources(self):
        """The number of things to sum over when computing cov.
        The default make_covariance_update_op function will call _compute_new_cov
        with indices ranging from 0 to _num_sources-1. The typical situation is
        where the factor wants to sum the statistics it computes over multiple
        backpropped "gradients" (typically passed in via "tensors" or
        "outputs_grads" arguments).
        """
        pass

    @abc.abstractproperty
    def _dtype(self):
        pass

    @property
    def _cov_initializer(self):
        return covariance_initializer

    def instantiate_covariance(self):
        """Instantiates the covariance Variable as the instance member _cov."""
        with variable_scope.variable_scope(self._var_scope):
            self._cov = variable_scope.get_variable(
                "cov",
                initializer=self._cov_initializer,
                shape=self._cov_shape,
                trainable=False,
                dtype=self._dtype)

    def re_init_covariance_op(self):
        new_cov = self._cov_initializer(shape=self._cov_shape,
                                        dtype=self._dtype,
                                        partition_info=None)
        return self._cov.assign(new_cov)

    @abc.abstractmethod
    def _compute_new_cov(self, idx=0):
        pass

    def make_covariance_update_op(self, ema_decay):
        """Constructs and returns the covariance update Op.
        Args:
          ema_decay: The exponential moving average decay (float or Tensor).
        Returns:
          An Op for updating the covariance Variable referenced by _cov.
        """
        new_cov = math_ops.add_n(
            tuple(self._compute_new_cov(idx) for idx in range(self._num_sources)))

        import tensorflow as tf
        # with tf.control_dependencies([tf.check_numerics(new_cov, message='check numerics - %d' % self._num_sources)]):
        return moving_averages.assign_moving_average(
            self._cov, new_cov, ema_decay, zero_debias=ZERO_DEBIAS)

    @abc.abstractmethod
    def make_inverse_update_ops(self):
        """Create and return update ops corresponding to registered computations."""
        pass

    def get_cov(self):
        return self._cov


class InverseProvidingFactor(FisherFactor):
    """Base class for FisherFactors that maintain inverses, powers, etc of _cov.
    Assumes that the _cov property is a square PSD matrix.
    Subclasses must implement the _compute_new_cov method, and the _var_scope and
    _cov_shape properties.
    """

    # TODO(b/69108481): This class (and its subclasses) should be refactored to
    # serve the matrix quantities it computes as both (potentially stale)
    # variables, updated by the inverse update ops, and fresh values stored in
    # tensors that recomputed once every session.run() call.  Currently matpower
    # and damp_inverse have the former behavior, while eigendecomposition has
    # the latter.

    def __init__(self):
        self._inverses_by_damping = {}
        self._matpower_by_exp_and_damping = {}
        self._eigendecomp = None

        super(InverseProvidingFactor, self).__init__()

    def register_damped_inverse(self, damping):
        """Registers a damped inverse needed by a FisherBlock.
        This creates a variable and signals make_inverse_update_ops to make the
        corresponding update op.  The variable can be read via the method
        get_inverse.
        Args:
          damping: The damping value (float or Tensor) for this factor.
        """
        if damping not in self._inverses_by_damping:
            damping_string = scalar_or_tensor_to_string(damping)
            with variable_scope.variable_scope(self._var_scope):
                inv = variable_scope.get_variable(
                    "inv_damp{}".format(damping_string),
                    initializer=inverse_initializer,
                    shape=self._cov_shape,
                    trainable=False,
                    dtype=self._dtype)
            self._inverses_by_damping[damping] = inv

    def register_matpower(self, exp, damping):
        """Registers a matrix power needed by a FisherBlock.
        This creates a variable and signals make_inverse_update_ops to make the
        corresponding update op.  The variable can be read via the method
        get_matpower.
        Args:
          exp: The exponent (float or Tensor) to raise the matrix to.
          damping: The damping value (float or Tensor).
        """
        if (exp, damping) not in self._matpower_by_exp_and_damping:
            exp_string = scalar_or_tensor_to_string(exp)
            damping_string = scalar_or_tensor_to_string(damping)
            with variable_scope.variable_scope(self._var_scope):
                matpower = variable_scope.get_variable(
                    "matpower_exp{}_damp{}".format(exp_string, damping_string),
                    initializer=inverse_initializer,
                    shape=self._cov_shape,
                    trainable=False,
                    dtype=self._dtype)
            self._matpower_by_exp_and_damping[(exp, damping)] = matpower

    def register_eigendecomp(self):
        """Registers an eigendecomposition.
        Unlike register_damp_inverse and register_matpower this doesn't create
        any variables or inverse ops.  Instead it merely makes tensors containing
        the eigendecomposition available to anyone that wants them.  They will be
        recomputed (once) for each session.run() call (when they needed by some op).
        """
        if not self._eigendecomp:
            eigenvalues, eigenvectors = linalg_ops.self_adjoint_eig(self._cov)

            # The matrix self._cov is positive semidefinite by construction, but the
            # numerical eigenvalues could be negative due to numerical errors, so here
            # we clip them to be at least FLAGS.eigenvalue_clipping_threshold
            clipped_eigenvalues = math_ops.maximum(eigenvalues,
                                                   EIGENVALUE_CLIPPING_THRESHOLD)
            self._eigendecomp = (clipped_eigenvalues, eigenvectors)

    def make_inverse_update_ops(self):
        """Create and return update ops corresponding to registered computations."""
        ops = []

        num_inverses = len(self._inverses_by_damping)
        matrix_power_registered = bool(self._matpower_by_exp_and_damping)
        use_eig = (
            self._eigendecomp or matrix_power_registered or
            num_inverses >= EIGENVALUE_DECOMPOSITION_THRESHOLD)

        if use_eig:
            self.register_eigendecomp()  # ensures self._eigendecomp is set
            eigenvalues, eigenvectors = self._eigendecomp  # pylint: disable=unpacking-non-sequence
            for damping, inv in self._inverses_by_damping.items():
                ops.append(
                    inv.assign(
                        math_ops.matmul(eigenvectors / (eigenvalues + damping),
                                        array_ops.transpose(eigenvectors))))

            for (exp, damping), matpower in self._matpower_by_exp_and_damping.items():
                ops.append(
                    matpower.assign(
                        math_ops.matmul(eigenvectors *
                                        (eigenvalues + damping)**exp,
                                        array_ops.transpose(eigenvectors))))
            # These ops share computation and should be run on a single device.
            ops = [control_flow_ops.group(*ops)]
        else:
            for damping, inv in self._inverses_by_damping.items():
                new_inv = utils.posdef_inv(self._cov, damping, INV_METHOD='cholesky')
                ops.append(inv.assign(new_inv))

        return ops

    def get_damped_inverse(self, damping):
        # Note that this function returns a variable which gets updated by the
        # inverse ops.  It may be stale / inconsistent with the latest value of
        # get_cov().
        return self._inverses_by_damping[damping]

    def get_matpower(self, exp, damping):
        # Note that this function returns a variable which gets updated by the
        # inverse ops.  It may be stale / inconsistent with the latest value of
        # get_cov().
        return self._matpower_by_exp_and_damping[(exp, damping)]

    def get_eigendecomp(self):
        # Unlike get_inverse and get_matpower this doesn't retrieve a stored
        # variable, but instead always computes a fresh version from the current
        # value of get_cov().
        return self._eigendecomp


class FullFactor(InverseProvidingFactor):
    """FisherFactor for a full matrix representation of the Fisher of a parameter.
    Note that this uses the naive "square the sum estimator", and so is applicable
    to any type of parameter in principle, but has very high variance.
    """

    def __init__(self,
                 params_grads,
                 batch_size,
                 colocate_cov_ops_with_inputs=False):
        self._batch_size = batch_size
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        self._orig_params_grads_name = scope_string_from_params(
            [params_grads, self._batch_size])
        params_grads_flat = []
        for params_grad in params_grads:
            with _maybe_colocate_with(params_grad,
                                      self._colocate_cov_ops_with_inputs):
                col = utils.tensors_to_column(params_grad)
                params_grads_flat.append(col)
        self._params_grads_flat = tuple(params_grads_flat)
        super(FullFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_full/" + self._orig_params_grads_name

    @property
    def _cov_shape(self):
        size = self._params_grads_flat[0].shape[0]
        return [size, size]

    @property
    def _num_sources(self):
        return len(self._params_grads_flat)

    @property
    def _dtype(self):
        return self._params_grads_flat[0].dtype

    def _compute_new_cov(self, idx=0):
        # This will be a very basic rank 1 estimate
        with _maybe_colocate_with(self._params_grads_flat[idx],
                                  self._colocate_cov_ops_with_inputs):
            return ((self._params_grads_flat[idx] * array_ops.transpose(
                self._params_grads_flat[idx])) / math_ops.cast(
                self._batch_size, self._params_grads_flat[idx].dtype))


class DiagonalFactor(FisherFactor):
    """A core class for FisherFactors that use diagonal approximations."""

    def __init__(self):
        super(DiagonalFactor, self).__init__()

    @property
    def _cov_initializer(self):
        return diagonal_covariance_initializer

    def make_inverse_update_ops(self):
        return []


class NaiveDiagonalFactor(DiagonalFactor):
    """FisherFactor for a diagonal approximation of any type of param's Fisher.
    Note that this uses the naive "square the sum estimator", and so is applicable
    to any type of parameter in principle, but has very high variance.
    """

    def __init__(self,
                 params_grads,
                 batch_size,
                 colocate_cov_ops_with_inputs=False):
        self._batch_size = batch_size
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        params_grads_flat = []
        for params_grad in params_grads:
            with _maybe_colocate_with(params_grad,
                                      self._colocate_cov_ops_with_inputs):
                col = utils.tensors_to_column(params_grad)
                params_grads_flat.append(col)
        self._params_grads = tuple(params_grads_flat)
        self._orig_params_grads_name = scope_string_from_params(
            [self._params_grads, self._batch_size])
        super(NaiveDiagonalFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_naivediag/" + self._orig_params_grads_name

    @property
    def _cov_shape(self):
        return self._params_grads[0].shape

    @property
    def _num_sources(self):
        return len(self._params_grads)

    @property
    def _dtype(self):
        return self._params_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._params_grads[idx],
                                  self._colocate_cov_ops_with_inputs):
            return (math_ops.square(self._params_grads[idx]) / math_ops.cast(
                self._batch_size, self._params_grads[idx].dtype))


class FullyConnectedDiagonalFactor(DiagonalFactor):
    r"""FisherFactor for a diagonal approx of a fully-connected layer's Fisher.
    Given in = [batch_size, input_size] and out_grad = [batch_size, output_size],
    approximates the covariance as,
      Cov(in, out) = (1/batch_size) \sum_{i} outer(in[i], out_grad[i]) ** 2.0
    where the square is taken element-wise.
    """

    # TODO(jamesmartens): add units tests for this class

    def __init__(self,
                 inputs,
                 outputs_grads,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Instantiate FullyConnectedDiagonalFactor.
        Args:
          inputs: Tensor of shape [batch_size, input_size]. Inputs to fully
            connected layer.
          outputs_grads: List of Tensors of shape [batch_size, output_size].
            Gradient of loss with respect to layer's preactivations.
          has_bias: bool. If True, append '1' to each input.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._outputs_grads = outputs_grads
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        self._batch_size = array_ops.shape(inputs)[0]
        self._orig_tensors_name = scope_string_from_params(
            (inputs,) + tuple(outputs_grads))

        # Note that we precompute the required operations on the inputs since the
        # inputs don't change with the 'idx' argument to _compute_new_cov.  (Only
        # the target entry of _outputs_grads changes with idx.)
        with _maybe_colocate_with(inputs, self._colocate_cov_ops_with_inputs):
            if has_bias:
                inputs = _append_homog(inputs)
            self._squared_inputs = math_ops.square(inputs)

        super(FullyConnectedDiagonalFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_diagfc/" + self._orig_tensors_name

    @property
    def _cov_shape(self):
        return [self._squared_inputs.shape[1], self._outputs_grads[0].shape[1]]

    @property
    def _num_sources(self):
        return len(self._outputs_grads)

    @property
    def _dtype(self):
        return self._outputs_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        # The well-known special formula that uses the fact that the entry-wise
        # square of an outer product is the outer-product of the entry-wise squares.
        # The gradient is the outer product of the input and the output gradients,
        # so we just square both and then take their outer-product.
        with _maybe_colocate_with(self._squared_inputs,
                                  self._colocate_cov_ops_with_inputs):
            new_cov = math_ops.matmul(
                self._squared_inputs,
                math_ops.square(self._outputs_grads[idx]),
                transpose_a=True)
            new_cov /= math_ops.cast(self._batch_size, new_cov.dtype)
            return new_cov


class ConvDiagonalFactor(DiagonalFactor):
    """FisherFactor for a diagonal approx of a convolutional layer's Fisher."""

    # TODO(jamesmartens): add units tests for this class

    def __init__(self,
                 inputs,
                 outputs_grads,
                 filter_shape,
                 strides,
                 padding,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Creates a ConvDiagonalFactor object.
        Args:
          inputs: Tensor of shape [batch_size, height, width, in_channels].
            Input activations to this layer.
          outputs_grads: Tensor of shape [batch_size, height, width, out_channels].
            Per-example gradients to the loss with respect to the layer's output
            preactivations.
          filter_shape: Tuple of 4 ints: (kernel_height, kernel_width, in_channels,
            out_channels). Represents shape of kernel used in this layer.
          strides: The stride size in this layer (1-D Tensor of length 4).
          padding: The padding in this layer (1-D of Tensor length 4).
          has_bias: Python bool. If True, the layer is assumed to have a bias
            parameter in addition to its filter parameter.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._filter_shape = filter_shape
        self._has_bias = has_bias
        self._outputs_grads = outputs_grads
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs

        self._orig_tensors_name = scope_string_from_name(
            (inputs,) + tuple(outputs_grads))

        # Note that we precompute the required operations on the inputs since the
        # inputs don't change with the 'idx' argument to _compute_new_cov.  (Only
        # the target entry of _outputs_grads changes with idx.)
        with _maybe_colocate_with(inputs, self._colocate_cov_ops_with_inputs):
            filter_height, filter_width, _, _ = self._filter_shape

            # TODO(b/64144716): there is potential here for a big savings in terms of
            # memory use.
            patches = array_ops.extract_image_patches(
                inputs,
                ksizes=[1, filter_height, filter_width, 1],
                strides=strides,
                rates=[1, 1, 1, 1],
                padding=padding)

            if has_bias:
                patches = _append_homog(patches)

            self._patches = patches

        super(ConvDiagonalFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_convdiag/" + self._orig_tensors_name

    @property
    def _cov_shape(self):
        filter_height, filter_width, in_channels, out_channels = self._filter_shape
        return [
            filter_height * filter_width * in_channels + self._has_bias,
            out_channels
        ]

    @property
    def _num_sources(self):
        return len(self._outputs_grads)

    @property
    def _dtype(self):
        return self._outputs_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._outputs_grads[idx],
                                  self._colocate_cov_ops_with_inputs):
            outputs_grad = self._outputs_grads[idx]
            batch_size = array_ops.shape(self._patches)[0]

            new_cov = self._convdiag_sum_of_squares(self._patches, outputs_grad)
            new_cov /= math_ops.cast(batch_size, new_cov.dtype)

            return new_cov

    def _convdiag_sum_of_squares(self, patches, outputs_grad):
        # This computes the sum of the squares of the per-training-case "gradients".
        # It does this simply by computing a giant tensor containing all of these,
        # doing an entry-wise square, and them summing along the batch dimension.
        case_wise_gradients = special_math_ops.einsum("bijk,bijl->bkl", patches,
                                                      outputs_grad)
        return math_ops.reduce_sum(math_ops.square(case_wise_gradients), axis=0)


class FullyConnectedKroneckerFactor(InverseProvidingFactor):
    """Kronecker factor for the input or output side of a fully-connected layer.
    """

    def __init__(self,
                 tensors,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Instantiate FullyConnectedKroneckerFactor.
        Args:
          tensors: List of Tensors of shape [batch_size, n]. Represents either a
            layer's inputs or its output's gradients.
          has_bias: bool. If True, append '1' to each row.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        # The tensor argument is either a tensor of input activations or a tensor of
        # output pre-activation gradients.
        self._has_bias = has_bias
        self._tensors = tensors
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        super(FullyConnectedKroneckerFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_fckron/" + scope_string_from_params(
            [self._tensors, self._has_bias])

    @property
    def _cov_shape(self):
        size = self._tensors[0].shape[1] + self._has_bias
        return [size, size]

    @property
    def _num_sources(self):
        return len(self._tensors)

    @property
    def _dtype(self):
        return self._tensors[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._tensors[idx],
                                  self._colocate_cov_ops_with_inputs):
            # import tensorflow as tf
            # with tf.control_dependencies([tf.check_numerics(self._tensors, message='check tensor original: has bias {}'.format(self._has_bias))]):
            tensor = self._tensors[idx]
            if self._has_bias:
                tensor = _append_homog(tensor)
            # return _compute_cov(tensor)
            # with tf.control_dependencies([tf.check_numerics(tensor, message='check tensor: has bias {}'.format(self._has_bias))]):
            new_cov = _compute_cov(tensor)
            # with tf.control_dependencies([tf.check_numerics(new_cov, message='check here: has bias {}'.format(self._has_bias))]):
            #     new_cov = new_cov + 0.
            return new_cov

class ConvInputKroneckerFactor(InverseProvidingFactor):
    r"""Kronecker factor for the input side of a convolutional layer.
    Estimates E[ a a^T ] where a is the inputs to a convolutional layer given
    example x. Expectation is taken over all examples and locations.
    Equivalent to \Omega in https://arxiv.org/abs/1602.01407 for details. See
    Section 3.1 Estimating the factors.
    """

    def __init__(self,
                 inputs,
                 filter_shape,
                 strides,
                 padding,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Initializes ConvInputKroneckerFactor.
        Args:
          inputs: Tensor of shape [batch_size, height, width, in_channels]. Inputs
            to layer.
          filter_shape: 1-D Tensor of length 4. Contains [kernel_height,
            kernel_width, in_channels, out_channels].
          strides: 1-D Tensor of length 4. Contains [batch_stride, height_stride,
            width_stride, in_channel_stride].
          padding: str. Padding method for layer. "SAME" or "VALID".
          has_bias: bool. If True, append 1 to in_channel.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._filter_shape = filter_shape
        self._strides = strides
        self._padding = padding
        self._has_bias = has_bias
        self._inputs = inputs
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        super(ConvInputKroneckerFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_convinkron/" + scope_string_from_params([
            self._inputs, self._filter_shape, self._strides, self._padding,
            self._has_bias
        ])

    @property
    def _cov_shape(self):
        filter_height, filter_width, in_channels, _ = self._filter_shape
        size = filter_height * filter_width * in_channels + self._has_bias
        return [size, size]

    @property
    def _num_sources(self):
        return 1

    @property
    def _dtype(self):
        return self._inputs.dtype

    def _compute_new_cov(self, idx=0):
        if idx != 0:
            raise ValueError("ConvInputKroneckerFactor only supports idx = 0")

        with _maybe_colocate_with(self._inputs, self._colocate_cov_ops_with_inputs):
            filter_height, filter_width, in_channels, _ = self._filter_shape

            patches = array_ops.extract_image_patches(
                self._inputs,
                ksizes=[1, filter_height, filter_width, 1],
                strides=self._strides,
                rates=[1, 1, 1, 1],
                padding=self._padding)

            flatten_size = (filter_height * filter_width * in_channels)
            patches_flat = array_ops.reshape(patches, [-1, flatten_size])

            if self._has_bias:
                patches_flat = _append_homog(patches_flat)

            return _compute_cov(patches_flat)


class ConvOutputKroneckerFactor(InverseProvidingFactor):
    r"""Kronecker factor for the output side of a convolutional layer.
    Estimates E[ ds ds^T ] where s is the preactivations of a convolutional layer
    given example x and ds = (d / d s) log(p(y|x, w)). Expectation is taken over
    all examples and locations.
    Equivalent to \Gamma in https://arxiv.org/abs/1602.01407 for details. See
    Section 3.1 Estimating the factors.
    """

    def __init__(self, outputs_grads, colocate_cov_ops_with_inputs=False):
        """Initializes ConvOutputKroneckerFactor.
        Args:
          outputs_grads: list of Tensors. Each Tensor is of shape
              [batch_size, height, width, out_channels].
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._out_channels = outputs_grads[0].shape.as_list()[3]
        self._outputs_grads = outputs_grads
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        super(ConvOutputKroneckerFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_convoutkron/" + scope_string_from_params(self._outputs_grads)

    @property
    def _cov_shape(self):
        size = self._out_channels
        return [size, size]

    @property
    def _num_sources(self):
        return len(self._outputs_grads)

    @property
    def _dtype(self):
        return self._outputs_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._outputs_grads[idx],
                                  self._colocate_cov_ops_with_inputs):
            reshaped_tensor = array_ops.reshape(self._outputs_grads[idx],
                                                [-1, self._out_channels])
            return _compute_cov(reshaped_tensor)


class EigenBasisProvidingFactor(FisherFactor):
    def __init__(self):
        self._eigen_basis = {}
        self._eigen_value = {}
        super(EigenBasisProvidingFactor, self).__init__()

    def register_eigen_basis(self, damping):
        """ With current value of covariance matrix, initialize Eigen-basis.
        :return: None
        """
        with variable_scope.variable_scope(self._var_scope):
            initial_values, initial_basis = \
                linalg_ops.self_adjoint_eig(self._cov +
                                            damping * linalg_ops.eye(self._cov.shape.as_list()[0]))
            eigen_basis = variable_scope.get_variable("basis",
                                                      initializer=initial_basis,
                                                      trainable=False,
                                                      dtype=self._dtype)
            eigen_value = variable_scope.get_variable("values",
                                                      initializer=initial_values,
                                                      trainable=False,
                                                      dtype=self._dtype)
            self._eigen_basis[damping] = eigen_basis
            self._eigen_value[damping] = eigen_value

    def make_eigen_basis_update_ops(self):
        ops = []
        with variable_scope.variable_scope(self._var_scope):
            for damping, eigen_basis in self._eigen_basis.items():
                new_value, new_basis = linalg_ops.self_adjoint_eig(self._cov +
                                                                   damping *
                                                                   linalg_ops.eye(self._cov.shape.as_list()[0]))
                ops.append(eigen_basis.assign(new_basis))
                ops.append(self._eigen_value[damping].assign(new_value))
            return control_flow_ops.group(*ops)

    def make_inverse_update_ops(self):
        raise NotImplementedError()

    def get_eigen_basis(self, damping):
        return self._eigen_basis[damping]

    def get_eigen_value(self, damping):
        return self._eigen_value[damping]


class FullyConnectedEigenBasisFactor(EigenBasisProvidingFactor):
    """Kronecker factor for the input or output side of a fully-connected layer.
    """

    def __init__(self,
                 tensors,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Instantiate FullyConnectedKroneckerFactor.
        Args:
          tensors: List of Tensors of shape [batch_size, n]. Represents either a
            layer's inputs or its output's gradients.
          has_bias: bool. If True, append '1' to each row.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        # The tensor argument is either a tensor of input activations or a tensor of
        # output pre-activation gradients.
        self._has_bias = has_bias
        self._tensors = tensors
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        super(FullyConnectedEigenBasisFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_eigen_basis_fc/" + scope_string_from_params(
            [self._tensors, self._has_bias])

    @property
    def _cov_shape(self):
        size = self._tensors[0].shape[1] + self._has_bias
        return [size, size]

    @property
    def _num_sources(self):
        return len(self._tensors)

    @property
    def _dtype(self):
        return self._tensors[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._tensors[idx],
                                  self._colocate_cov_ops_with_inputs):
            tensor = self._tensors[idx]
            if self._has_bias:
                tensor = _append_homog(tensor)
            return _compute_cov(tensor)


class ConvInputEigenBasisKroneckerFactor(EigenBasisProvidingFactor):
    r"""E-cprrected Kronecker factor for the input side of a convolutional layer.
    Estimates E[ a a^T ] where a is the inputs to a convolutional layer given
    example x. Expectation is taken over all examples and locations.
    Equivalent to \Omega in https://arxiv.org/abs/1602.01407 for details. See
    Section 3.1 Estimating the factors.
    """
    def __init__(self,
                 inputs,
                 filter_shape,
                 strides,
                 padding,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Initializes ConvInputKroneckerFactor.
        Args:
          inputs: Tensor of shape [batch_size, height, width, in_channels]. Inputs
            to layer.
          filter_shape: 1-D Tensor of length 4. Contains [kernel_height,
            kernel_width, in_channels, out_channels].
          strides: 1-D Tensor of length 4. Contains [batch_stride, height_stride,
            width_stride, in_channel_stride].
          padding: str. Padding method for layer. "SAME" or "VALID".
          has_bias: bool. If True, append 1 to in_channel.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._filter_shape = filter_shape
        self._strides = strides
        self._padding = padding
        self._has_bias = has_bias
        self._inputs = inputs
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        super(ConvInputEigenBasisKroneckerFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_convin_eigen_basis/" + scope_string_from_params([
            self._inputs, self._filter_shape, self._strides, self._padding,
            self._has_bias
        ])

    @property
    def _cov_shape(self):
        filter_height, filter_width, in_channels, _ = self._filter_shape
        size = filter_height * filter_width * in_channels + self._has_bias
        return [size, size]

    @property
    def _num_sources(self):
        return 1

    @property
    def _dtype(self):
        return self._inputs.dtype

    def _compute_new_cov(self, idx=0):
        if idx != 0:
            raise ValueError("ConvInputKroneckerFactor only supports idx = 0")

        with _maybe_colocate_with(self._inputs, self._colocate_cov_ops_with_inputs):
            filter_height, filter_width, in_channels, _ = self._filter_shape

            patches = array_ops.extract_image_patches(
                self._inputs,
                ksizes=[1, filter_height, filter_width, 1],
                strides=self._strides,
                rates=[1, 1, 1, 1],
                padding=self._padding)

            flatten_size = (filter_height * filter_width * in_channels)
            patches_flat = array_ops.reshape(patches, [-1, flatten_size])

            if self._has_bias:
                patches_flat = _append_homog(patches_flat)

            return _compute_cov(patches_flat)


class ConvOutputEigenBasisFactor(EigenBasisProvidingFactor):
    r"""E-corrected Kronecker factor for the output side of a convolution layer.
    Estimates E[ ds ds^T ] where s is the preactivations of a convolutional layer
    given example x and ds = (d / d s) log(p(y|x, w)). Expectation is taken over
    all examples and locations.
    Equivalent to \Gamma in https://arxiv.org/abs/1602.01407 for details. See
    Section 3.1 Estimating the factors.
    """

    def __init__(self, outputs_grads, colocate_cov_ops_with_inputs=False):
        """Initializes ConvOutputKroneckerFactor.
        Args:
          outputs_grads: list of Tensors. Each Tensor is of shape
              [batch_size, height, width, out_channels].
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._out_channels = outputs_grads[0].shape.as_list()[3]
        self._outputs_grads = outputs_grads
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        super(ConvOutputEigenBasisFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_convout_eigen_basis/" + scope_string_from_params(self._outputs_grads)

    @property
    def _cov_shape(self):
        size = self._out_channels
        return [size, size]

    @property
    def _num_sources(self):
        return len(self._outputs_grads)

    @property
    def _dtype(self):
        return self._outputs_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._outputs_grads[idx],
                                  self._colocate_cov_ops_with_inputs):
            reshaped_tensor = array_ops.reshape(self._outputs_grads[idx],
                                                [-1, self._out_channels])
            return _compute_cov(reshaped_tensor)


class ScaleFactor(FisherFactor):
    """ A core class for FisherFactors that stores scaling information. """

    def __init__(self):
        """ Initialize class ScaleFactor.
        """
        self._input_factor_eigen_basis = None
        self._output_factor_eigen_basis = None
        self._input_factor_eigen_value = None
        self._output_factor_eigen_value = None

        super(ScaleFactor, self).__init__()

    def register_eigen_basis(self, input_factor_eigen_basis, output_factor_eigen_basis,
                             input_factor_eigen_value, output_factor_eigen_value,
                             renom_coeff=1.0):
        """ Registers an input and output Eigen Basis that is necessary
        in computing cov_update_ops.
        :return: None
        """
        with variable_scope.variable_scope(self._var_scope):
            # Rather than creating a new variable, link the Eigen Basis.
            self._input_factor_eigen_basis = input_factor_eigen_basis
            self._output_factor_eigen_basis = output_factor_eigen_basis
            self._input_factor_eigen_value = input_factor_eigen_value
            self._output_factor_eigen_value = output_factor_eigen_value
            self._renom_coeff = renom_coeff

    def init_kfac_scale_factor_op(self):
        input_scale = array_ops.expand_dims(self._input_factor_eigen_value, -1)
        # -> input_size x 1
        output_scale = array_ops.expand_dims(self._output_factor_eigen_value, -1)
        # -> output_size x 1
        # Outer product on scale factors.
        new_scale = math_ops.matmul(input_scale, output_scale, transpose_b=True)
        # -> input_size x output_size
        return self._cov.assign(new_scale * self._renom_coeff)

    @property
    def _cov_initializer(self):
        return diagonal_covariance_initializer

    def make_inverse_update_ops(self):
        raise NotImplementedError

    def make_eigen_basis_update_ops(self):
        raise NotImplementedError


class FullyConnectedScaleFactor(ScaleFactor):
    def __init__(self,
                 inputs,
                 outputs_grads,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Instantiate FullyConnectedScaleFactor.
        :param inputs: Tensor of shape [batch_size, input_size]. Inputs to fully
            connected layer.
        :param outputs_grads: List of Tensors of shape [batch_size, output_size].
        :param has_bias: bool. If True, append '1' to each input.
        :param colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._outputs_grads = outputs_grads
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs
        self._batch_size = array_ops.shape(inputs)[0]
        self._orig_tensors_name = scope_string_from_params(
            (inputs,) + tuple(outputs_grads))

        # Note that we precompute the required operations on the inputs since the
        # inputs don't change with the 'idx' argument to _compute_new_cov.  (Only
        # the target entry of _outputs_grads changes with idx.)
        with _maybe_colocate_with(inputs, self._colocate_cov_ops_with_inputs):
            if has_bias:
                inputs = _append_homog(inputs)
            self._inputs = inputs

        super(FullyConnectedScaleFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_scalefc/" + self._orig_tensors_name

    @property
    def _cov_shape(self):
        return [self._inputs.shape[1], self._outputs_grads[0].shape[1]]

    @property
    def _num_sources(self):
        return len(self._outputs_grads)

    @property
    def _dtype(self):
        return self._outputs_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._inputs,
                                  self._colocate_cov_ops_with_inputs):

            transformed_inputs = math_ops.matmul(self._inputs,
                                                 self._input_factor_eigen_basis)
            transformed_outputs_grads = math_ops.matmul(self._outputs_grads[idx],
                                                        self._output_factor_eigen_basis)
            new_cov = math_ops.matmul(
                math_ops.square(transformed_inputs),
                math_ops.square(transformed_outputs_grads),
                transpose_a=True)
            new_cov /= math_ops.cast(self._batch_size, new_cov.dtype)

            return new_cov


class ConvScaleFactor(ScaleFactor):
    """FisherFactor for a diagonal approx of a convolutional layer's Fisher."""

    def __init__(self,
                 inputs,
                 outputs_grads,
                 filter_shape,
                 strides,
                 padding,
                 has_bias=False,
                 colocate_cov_ops_with_inputs=False):
        """Creates a ConvDiagonalFactor object.
        Args:
          inputs: Tensor of shape [batch_size, height, width, in_channels].
            Input activations to this layer.
          outputs_grads: Tensor of shape [batch_size, height, width, out_channels].
            Per-example gradients to the loss with respect to the layer's output
            preactivations.
          filter_shape: Tuple of 4 ints: (kernel_height, kernel_width, in_channels,
            out_channels). Represents shape of kernel used in this layer.
          strides: The stride size in this layer (1-D Tensor of length 4).
          padding: The padding in this layer (1-D of Tensor length 4).
          has_bias: Python bool. If True, the layer is assumed to have a bias
            parameter in addition to its filter parameter.
          colocate_cov_ops_with_inputs: Whether to colocate cov_update ops with
              their inputs.
        """
        self._filter_shape = filter_shape
        self._has_bias = has_bias
        self._outputs_grads = outputs_grads
        self._colocate_cov_ops_with_inputs = colocate_cov_ops_with_inputs

        self._orig_tensors_name = scope_string_from_name(
            (inputs,) + tuple(outputs_grads))

        # Note that we precompute the required operations on the inputs since the
        # inputs don't change with the 'idx' argument to _compute_new_cov.  (Only
        # the target entry of _outputs_grads changes with idx.)
        with _maybe_colocate_with(inputs, self._colocate_cov_ops_with_inputs):
            filter_height, filter_width, _, _ = self._filter_shape

            patches = array_ops.extract_image_patches(
                inputs,
                ksizes=[1, filter_height, filter_width, 1],
                strides=strides,
                rates=[1, 1, 1, 1],
                padding=padding)

            if has_bias:
                patches = _append_homog(patches)

            self._patches = patches

        super(ConvScaleFactor, self).__init__()

    @property
    def _var_scope(self):
        return "ff_conv_scale/" + self._orig_tensors_name

    @property
    def _cov_shape(self):
        filter_height, filter_width, in_channels, out_channels = self._filter_shape
        return [
            filter_height * filter_width * in_channels + self._has_bias,
            out_channels
        ]

    @property
    def _num_sources(self):
        return len(self._outputs_grads)

    @property
    def _dtype(self):
        return self._outputs_grads[0].dtype

    def _compute_new_cov(self, idx=0):
        with _maybe_colocate_with(self._outputs_grads[idx],
                                  self._colocate_cov_ops_with_inputs):
            batch_size = array_ops.shape(self._patches)[0]
            batch_size = array_ops.shape(self._patches)[0]
            transformed_inputs = special_math_ops.einsum("bijk,kl->bijl",
                                                         self._patches,
                                                         self._input_factor_eigen_basis)
            transformed_outputs_grads = special_math_ops.einsum("bijk,kl->bijl",
                                                                self._outputs_grads[idx],
                                                                self._output_factor_eigen_basis)
            new_scale = special_math_ops.einsum("bijk,bijl->bkl",
                                                transformed_inputs,
                                                transformed_outputs_grads)
            new_cov = math_ops.reduce_sum(math_ops.square(new_scale), axis=0)
            new_cov /= math_ops.cast(batch_size, new_scale.dtype)

            return new_cov