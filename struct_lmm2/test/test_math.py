import pytest
from numpy import array, eye
from numpy.testing import assert_allclose
from numpy.random import RandomState
from struct_lmm2._math import (
    P_matrix,
    score_statistic,
    score_statistic_dist_weights,
    score_statistic_liu_params,
)


@pytest.fixture
def data():
    random = RandomState(0)
    n_samples = 3
    n_covariates = 2

    W = random.randn(n_samples, n_covariates)

    K0 = random.randn(n_samples, n_samples)
    K0 = K0 @ K0.T

    v = 0.2
    K = v * K0 + eye(n_samples)
    dK = K0

    alpha = array([0.5, -0.2])
    y = random.multivariate_normal(W @ alpha, K)

    return {"y": y, "W": W, "K": K, "dK": dK}


def test_P_matrix(data):
    P = array(
        [
            [0.50355613, -0.24203676, -0.34880245],
            [-0.24203676, 0.11633617, 0.16765363],
            [-0.34880245, 0.16765363, 0.24160792],
        ]
    )
    assert_allclose(P_matrix(data["W"], data["K"]), P)


def test_score_statistic(data):
    q = score_statistic(data["y"], data["W"], data["K"], data["dK"])
    assert_allclose(q, 0.49961017073389324)


def test_score_statistic_dist_weights(data):
    weights = score_statistic_dist_weights(data["W"], data["K"], data["dK"])
    assert_allclose(weights, array([4.55266277e-09, 3.46249449e-01]))


def test_score_statistic_liu_params(data):
    q = score_statistic(data["y"], data["W"], data["K"], data["dK"])
    weights = score_statistic_dist_weights(data["W"], data["K"], data["dK"])
    params = score_statistic_liu_params(q, weights)
    assert_allclose(params["pv"], 0.22966744652848403)
    assert_allclose(params["mu_q"], 0.34624945394475326)
    assert_allclose(params["sigma_q"], 0.48967066729451103)
    assert_allclose(params["dof_x"], 1.0)