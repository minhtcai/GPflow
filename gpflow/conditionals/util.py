from typing import Callable

import tensorflow as tf

from ..util import create_logger, default_jitter

logger = create_logger()


def base_conditional(
        Kmn: tf.Tensor,
        Kmm: tf.Tensor,
        Knn: tf.Tensor,
        function: tf.Tensor,
        *, full_cov=False, q_sqrt=None, white=False):
    """
    Given a g1 and g2, and distribution p and q such that
      p(g2) = N(g2;0,Kmm)
      p(g1) = N(g1;0,Knn)
      p(g1|g2) = N(g1;0,Knm)
    And
      q(g2) = N(g2;f,q_sqrt*q_sqrt^T)
    This method computes the mean and (co)variance of
      q(g1) = \int q(g2) p(g1|g2)
    :param Kmn: [M, N]
    :param Kmm: [M, M]
    :param Knn: [N, N] or [N]
    :param function: [M, R]
    :param full_cov: bool
    :param q_sqrt: None or [R, M, M] (lower triangular)
    :param white: bool
    :return: [N, R]  or [R, N, N]
    """
    logger.debug("base conditional")
    # compute kernel stuff
    num_func = function.shape[1]  # R
    N = Kmn.shape[-1]
    M = function.shape[0]

    # get the leadings dims in Kmn to the front of the tensor
    # if Kmn has rank two, i.e. [M, N], this is the identity op.
    Kmn_rank = tf.rank(Kmn)
    leading_indices = tf.range(1, Kmn_rank - 1)
    permute_indices = tf.concat([leading_indices, [0], [Kmn_rank - 1]], 0)  # [Kmn_rank]
    Kmn = tf.transpose(Kmn, permute_indices)  # [..., M, N]

    leading_dims = Kmn.shape[:-2]
    Lm = tf.linalg.cholesky(Kmm)  # [M, M]

    # Compute the projection matrix A
    Lm = tf.broadcast_to(Lm, tf.concat([leading_dims, Lm.shape], 0))  # [..., M, M]
    A = tf.linalg.triangular_solve(Lm, Kmn, lower=True)  # [..., M, N]

    # compute the covariance due to the conditioning
    if full_cov:
        fvar = Knn - tf.matmul(A, A, transpose_a=True)  # [..., N, N]
        cov_shape = tf.concat([leading_dims, [num_func, N, N]], 0)
        fvar = tf.broadcast_to(tf.expand_dims(fvar, -3), cov_shape)  # [..., R, N, N]
    else:
        fvar = Knn - tf.reduce_sum(tf.square(A), -2)  # [...,N]
        cov_shape = tf.concat([leading_dims, [num_func, N]], 0)  # [..., R, N]
        fvar = tf.broadcast_to(tf.expand_dims(fvar, -2), cov_shape)  # [..., R, N]

    # another backsubstitution in the unwhitened case
    if not white:
        A = tf.linalg.triangular_solve(tf.linalg.transpose(Lm), A, lower=False)

    # construct the conditional mean
    f_shape = tf.concat([leading_dims, [M, num_func]], 0)  # [..., M, R]
    function = tf.broadcast_to(function, f_shape)  # [..., M, R]
    fmean = tf.matmul(A, function, transpose_a=True)  # [..., N, R]

    if q_sqrt is not None:
        q_sqrt_dims = q_sqrt.shape.ndims
        if q_sqrt_dims == 2:
            LTA = A * tf.expand_dims(tf.transpose(q_sqrt), 2)  # [R, M, N]
        elif q_sqrt_dims == 3:
            L = q_sqrt
            L = tf.broadcast_to(L, tf.concat([leading_dims, L.shape], 0))
            shape = tf.concat([leading_dims, [num_func, M, N]], 0)
            A = tf.broadcast_to(tf.expand_dims(A, -3), shape)
            LTA = tf.matmul(L, A, transpose_a=True)  # [R, M, N]
        else:  # pragma: no cover
            raise ValueError(f"Bad dimension for q_sqrt: {q_sqrt_dims}")

        if full_cov:
            fvar = fvar + tf.matmul(LTA, LTA, transpose_a=True)  # [R, N, N]
        else:
            fvar = fvar + tf.reduce_sum(tf.square(LTA), -2)  # [R, N]

    if not full_cov:
        fvar = tf.linalg.transpose(fvar)  # [N, R]

    return fmean, fvar  # [N, R], [R, N, N] or [N, R]


def sample_mvn(mean, cov, cov_structure):
    """
    Returns a sample from a D-dimensional Multivariate Normal distribution
    :param mean: N x D
    :param cov: N x D or N x D x D
    :param cov_structure: "diag" or "full"
    - "diag": cov holds the diagonal elements of the covariance matrix
    - "full": cov holds the full covariance matrix (without jitter)
    :return: sample from the MVN of shape N x D
    """
    eps = tf.random_normal(mean.shape, dtype=mean.dtype)  # N x P
    if cov_structure == "diag":
        sample = mean + tf.sqrt(cov) * eps  # N x P
    elif cov_structure == "full":
        cov = cov + (tf.eye(mean.shape[1], dtype=mean.dtype) * default_jitter())[None, ...]  # N x P x P
        chol = tf.linalg.cholesky(cov)  # N x P x P
        return mean + (tf.matmul(chol, eps[..., None])[..., 0])  # N x P
    else:
        raise NotImplementedError

    return sample  # N x P


def expand_independent_outputs(fvar, full_cov, full_output_cov):
    """
    Reshapes fvar to the correct shape, specified by `full_cov` and `full_output_cov`.

    :param fvar: has shape N x P (full_cov = False) or P x N x N (full_cov = True).
    :return:
    1. full_cov: True and full_output_cov: True
       fvar N x P x N x P
    2. full_cov: True and full_output_cov: False
       fvar P x N x N
    3. full_cov: False and full_output_cov: True
       fvar N x P x P
    4. full_cov: False and full_output_cov: False
       fvar N x P
    """
    if full_cov and full_output_cov:
        fvar = tf.linalg.diag(tf.transpose(fvar))   # N x N x P x P
        fvar = tf.transpose(fvar, [0, 2, 1, 3])  # N x P x N x P
    if not full_cov and full_output_cov:
        fvar = tf.linalg.diag(fvar)   # N x P x P
    if full_cov and not full_output_cov:
        pass  # P x N x N
    if not full_cov and not full_output_cov:
        pass  # N x P

    return fvar


def independent_interdomain_conditional(Kmn, Kmm, Knn, f, *, full_cov=False, full_output_cov=False,
                                        q_sqrt=None, white=False):
    """
    The inducing outputs live in the g-space (R^L).
    Interdomain conditional calculation.

    :param Kmn: M x L x N x P
    :param Kmm: L x M x M
    :param Knn: N x P  or  N x N  or  P x N x N  or  N x P x N x P
    :param f: data matrix, M x L
    :param q_sqrt: L x M x M  or  M x L
    :param full_cov: calculate covariance between inputs
    :param full_output_cov: calculate covariance between outputs
    :param white: use whitened representation
    :return:
        - mean: N x P
        - variance: N x P, N x P x P, P x N x N, N x P x N x P
    """
    logger.debug("independent_interdomain_conditional")
    M, L, N, P = [Kmn.shape[i] for i in range(Kmn.shape.ndims)]

    Lm = tf.linalg.cholesky(Kmm)  # L x M x M

    # Compute the projection matrix A
    Kmn = tf.reshape(tf.transpose(Kmn, (1, 0, 2, 3)), (L, M, N * P))
    A = tf.linalg.triangular_solve(Lm, Kmn, lower=True)  # L x M x M  *  L x M x NP  ->  L x M x NP
    Ar = tf.reshape(A, (L, M, N, P))

    # compute the covariance due to the conditioning
    if full_cov and full_output_cov:
        fvar = Knn - tf.tensordot(Ar, Ar, [[0, 1], [0, 1]])  # N x P x N x P
    elif full_cov and not full_output_cov:
        At = tf.reshape(tf.transpose(Ar), (P, N, M * L))  # P x N x ML
        fvar = Knn - tf.matmul(At, At, transpose_b=True)  # P x N x N
    elif not full_cov and full_output_cov:
        At = tf.reshape(tf.transpose(Ar, [2, 3, 1, 0]), (N, P, M * L))  # N x P x ML
        fvar = Knn - tf.matmul(At, At, transpose_b=True)  # N x P x P
    elif not full_cov and not full_output_cov:
        fvar = Knn - tf.reshape(tf.reduce_sum(tf.square(A), [0, 1]), (N, P))  # Knn: N x P

    # another backsubstitution in the unwhitened case
    if not white:
        A = tf.linalg.triangular_solve(Lm, Ar)  # L x M x M  *  L x M x NP  ->  L x M x NP
        Ar = tf.reshape(A, (L, M, N, P))

    fmean = tf.tensordot(Ar, f, [[1, 0], [0, 1]])  # N x P

    if q_sqrt is not None:
        if q_sqrt.shape.ndims == 3:
            Lf = tf.matrix_band_part(q_sqrt, -1, 0)  # L x M x M
            LTA = tf.matmul(Lf, A, transpose_a=True)  # L x M x M  *  L x M x NP  ->  L x M x NP
        else:  # q_sqrt M x L
            LTA = (A * tf.transpose(q_sqrt)[..., None])  # L x M x NP

        if full_cov and full_output_cov:
            LTAr = tf.reshape(LTA, (L * M, N * P))
            fvar = fvar + tf.reshape(tf.matmul(LTAr, LTAr, transpose_a=True), (N, P, N, P))
        elif full_cov and not full_output_cov:
            LTAr = tf.transpose(tf.reshape(LTA, (L * M, N, P)), [2, 0, 1])  # P x LM x N
            fvar = fvar + tf.matmul(LTAr, LTAr, transpose_a=True)  # P x N x N
        elif not full_cov and full_output_cov:
            LTAr = tf.transpose(tf.reshape(LTA, (L * M, N, P)), [1, 0, 2])  # N x LM x P
            fvar = fvar + tf.matmul(LTAr, LTAr, transpose_a=True)  # N x P x P
        elif not full_cov and not full_output_cov:
            fvar = fvar + tf.reshape(tf.reduce_sum(tf.square(LTA), (0, 1)), (N, P))
    return fmean, fvar


def fully_correlated_conditional(Kmn, Kmm, Knn, f, *, full_cov=False, full_output_cov=False, q_sqrt=None, white=False):
    """
    This function handles conditioning of multi-output GPs in the case where the conditioning
    points are all fully correlated, in both the prior and posterior.
    :param Kmn: LM x N x P
    :param Kmm: LM x LM
    :param Knn: N x P or N x P x N x P
    :param f: data matrix, LM x 1
    :param q_sqrt: 1 x LM x LM  or 1 x ML
    :param full_cov: calculate covariance between inputs
    :param full_output_cov: calculate covariance between outputs
    :param white: use whitened representation
    :return:
        - mean: N x P
        - variance: N x P, N x P x P, P x N x N, N x P x N x P
    """
    m, v = fully_correlated_conditional_repeat(Kmn, Kmm, Knn, f, full_cov=full_cov,
                                               full_output_cov=full_output_cov, q_sqrt=q_sqrt, white=white)
    return m[0, ...], v[0, ...]


def fully_correlated_conditional_repeat(Kmn, Kmm, Knn, f, *, full_cov=False, full_output_cov=False, q_sqrt=None,
                                        white=False):
    """
    This function handles conditioning of multi-output GPs in the case where the conditioning
    points are all fully correlated, in both the prior and posterior.

    Note: This conditional can handle 'repetitions' R, given in `f` and `q_sqrt`.

    :param Kmn: LM x N x P
    :param Kmm: LM x LM
    :param Knn: N x P or N x P x N x P
    :param f: data matrix, LM x R
    :param q_sqrt: R x LM x LM  or R x ML
    :param full_cov: calculate covariance between inputs
    :param full_output_cov: calculate covariance between outputs
    :param white: use whitened representation
    :return:
        - mean: R x N x P
        - variance: R x N x P, R x N x P x P, R x P x N x N, R x N x P x N x P
    """
    logger.debug("fully correlated conditional")
    R = f.shape[1]
    M, N, K = [Kmn.shape[i] for i in range(Kmn.shape.ndims)]
    Lm = tf.linalg.cholesky(Kmm)

    # Compute the projection matrix A
    # Lm: M x M    Kmn: M x NK
    Kmn = tf.reshape(Kmn, (M, N * K))  # M x NK
    A = tf.linalg.triangular_solve(Lm, Kmn, lower=True)  # M x NK
    Ar = tf.reshape(A, (M, N, K))

    # compute the covariance due to the conditioning
    if full_cov and full_output_cov:
        # fvar = Knn - tf.matmul(Ar, Ar, transpose_a=True)  # NK x NK, then reshape?
        fvar = Knn - tf.tensordot(Ar, Ar, [[0], [0]])  # N x K x N x K
    elif full_cov and not full_output_cov:
        At = tf.transpose(Ar)  # K x N x M
        fvar = Knn - tf.matmul(At, At, transpose_b=True)  # K x N x N
    elif not full_cov and full_output_cov:
        # This transpose is annoying
        At = tf.transpose(Ar, [1, 0, 2])  # N x M x K
        # fvar = Knn - tf.einsum('mnk,mnl->nkl', Ar, Ar)
        fvar = Knn - tf.matmul(At, At, transpose_a=True)  # N x K x K
    elif not full_cov and not full_output_cov:
        # Knn: N x K
        fvar = Knn - tf.reshape(tf.reduce_sum(tf.square(A), [0, 1]), (N, K))  # Can also do this with a matmul

    # another backsubstitution in the unwhitened case
    if not white:
        # A = tf.linalg.triangular_solve(tf.linalg.transpose(Lm), A, lower=False)  # M x NK
        raise NotImplementedError("Need to verify this.")  # pragma: no cover

    # f: M x R
    fmean = tf.matmul(f, A, transpose_a=True)  # R x M  *  M x NK  ->  R x NK
    fmean = tf.reshape(fmean, (R, N, K))  # R x N x K

    if q_sqrt is not None:
        Lf = tf.matrix_band_part(q_sqrt, -1, 0)  # R x M x M
        if q_sqrt.shape.ndims == 3:
            A_tiled = tf.tile(A[None, :, :], tf.stack([R, 1, 1]))  # [R, M, N]K
            LTA = tf.matmul(Lf, A_tiled, transpose_a=True)  # [R, M, N]K
        elif q_sqrt.shape.ndims == 2:  # pragma: no cover
            raise NotImplementedError("Does not support diagonal q_sqrt yet...")
        else:  # pragma: no cover
            raise ValueError(f"Bad dimension for q_sqrt: {q_sqrt.shape.ndims}")

        if full_cov and full_output_cov:
            addvar = tf.matmul(LTA, LTA, transpose_a=True)  # R x NK x NK
            fvar = fvar[None, :, :, :, :] + tf.reshape(addvar, (R, N, K, N, K))
        elif full_cov and not full_output_cov:
            LTAr = tf.transpose(tf.reshape(LTA, [R, M, N, K]), [0, 3, 1, 2])  # R x K x M x N
            addvar = tf.matmul(LTAr, LTAr, transpose_a=True)  # R x K x N x N
            fvar = fvar[None, ...] + addvar  # R x K x N x N
        elif not full_cov and full_output_cov:
            LTAr = tf.transpose(tf.reshape(LTA, (R, M, N, K)), [0, 2, 3, 1])  # R x N x K x M
            fvar = fvar[None, ...] + tf.matmul(LTAr, LTAr, transpose_b=True)  # R x N x K x K
        elif not full_cov and not full_output_cov:
            addvar = tf.reshape(tf.reduce_sum(LTA ** 2, axis=1), (R, N, K))  # R x N x K
            fvar = fvar[None, ...] + addvar  # R x N x K
    return fmean, fvar