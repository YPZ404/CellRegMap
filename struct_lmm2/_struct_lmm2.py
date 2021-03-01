from typing import Optional

from glimix_core.lmm import LMM, Kron2Sum
from numpy import asarray, concatenate, inf, linspace, newaxis, sqrt, vstack
from numpy.linalg import multi_dot
from numpy_sugar import ddot
from numpy_sugar.linalg import economic_qs_linear
from tqdm import tqdm

from ._math import PMat, QSCov, ScoreStatistic


class StructLMM2:
    """
    Mixed-model with genetic effect heterogeneity.

    The extended StructLMM model (two random effects) is:

        𝐲 = W𝛂 + 𝐠⊙𝛃 + 𝐞 + 𝐮 + 𝛆,                                              (1)

    where:

        𝛃 ~ 𝓝(𝟎, 𝓋₀((1-ρ₀)𝟏𝟏ᵀ + ρ₀𝙴𝙴ᵀ)),
        𝐞 ~ 𝓝(𝟎, 𝓋₁ρ₁𝙴𝙴ᵀ),
        𝐮 ~ 𝓝(𝟎, 𝓋₁(1-ρ₁)𝙺), and
        𝛆 ~ 𝓝(𝟎, 𝓋₂𝙸).

    𝐠⊙𝛃 is made of two components: the persistent genotype effect and the GxE effect. 𝐞 is the
    environment effect, 𝐮 is the population structure effect, and 𝛆 is the iid noise. The full
    covariance of 𝐲 is therefore given by:

        cov(𝐲) = 𝓋₀(1-ρ₀)𝙳𝟏𝟏ᵀ𝙳 + 𝓋₀ρ₀𝙳𝙴𝙴ᵀ𝙳 + 𝓋₁ρ₁𝙴𝙴ᵀ + 𝓋₁(1-ρ₁)𝙺 + 𝓋₂𝙸,

    where 𝙳 = diag(𝐠). Its marginalised form is given by:

        𝐲 ~ 𝓝(W𝛂, 𝓋₀𝙳((1-ρ₀)𝟏𝟏ᵀ + ρ₀𝙴𝙴ᵀ)𝙳 + 𝓋₁(ρ₁𝙴𝙴ᵀ + (1-ρ₁)𝙺) + 𝓋₂𝙸).

    StructLMM method is used to perform two types of statistical tests.

    1. The association test compares the following hypotheses (from Eq. 1):

        𝓗₀: 𝓋₀ = 0
        𝓗₁: 𝓋₀ > 0

    𝓗₀ denotes no genetic association, while 𝓗₁ models any genetic association. In particular, 𝓗₁
    includes genotype-environment interaction as part of genetic association.

    2. The interaction test is slighlty different as the persistent genotype effect is now
    considered to be a fixed effect, and added to the model as an additional covariate term:

        𝐲 = W𝛂 + 𝐠𝛽₁ + 𝐠⊙𝛃₂ + 𝐞 + 𝐮 + 𝛆,                                       (2)

    where:

        𝛃₂ ~ 𝓝(𝟎, 𝓋₃𝙴𝙴ᵀ),
        𝐞  ~ 𝓝(𝟎, 𝓋₁ρ₁𝙴𝙴ᵀ),
        𝐮  ~ 𝓝(𝟎, 𝓋₁(1-ρ₁)𝙺), and
        𝛆  ~ 𝓝(𝟎, 𝓋₂𝙸).

    We refer to this modified model as the interaction model. The compared hypotheses in this case
    are:

        𝓗₀: 𝓋₃ = 0
        𝓗₁: 𝓋₃ > 0
    """

    def __init__(self, y, W, E, G=[]):
        # TODO: convert y to nx0
        # TODO: convert W to nxp
        # TODO: convert to array of floats
        self._y = y
        self._W = W
        self._E = E
        self._G = G
        # self._EE = E @ E.T

        self._null_lmm_assoc = {}

        self._halfSigma = {}
        self._Sigma_qs = {}
        # TODO: remove it after debugging
        self._Sigma = {}

        if len(G) == 0:
            self._rho0 = [1.0]
            self._rho1 = [1.0]
            self._halfSigma[1.0] = self._E
            self._Sigma_qs[1.0] = economic_qs_linear(self._E, return_q1=False)
        else:
            self._rho0 = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
            self._rho1 = linspace(0, 1, 10)
            for rho1 in self._rho1:
                # Σ = ρ₁𝙴𝙴ᵀ + (1-ρ₁)𝙺
                # concatenate((sqrt(rho1) * self._E, sqrt(1 - rho1) * G1), axis=1)
                # self._Sigma[rho1] = rho1 * self._EE + (1 - rho1) * self._K
                # self._Sigma_qs[rho1] = economic_qs(self._Sigma[rho1])
                a = sqrt(rho1)
                b = sqrt(1 - rho1)
                hS = concatenate([a * self._E] + [b * Gi for Gi in G], axis=1)
                self._halfSigma[rho1] = hS
                self._Sigma_qs[rho1] = economic_qs_linear(
                    self._halfSigma[rho1], return_q1=False
                )
                # TODO: remove me, it is for debugging
                # tmp = sum([Gi @ Gi.T for Gi in G])
                # self._Sigma[rho1] = rho1 * self._E @ self._E.T + (1 - rho1) * tmp

    @property
    def _n_samples(self):
        return self._y.shape[0]

    def fit_null_association(self):
        """
        Fit p(𝐲) of Eq. (1) under the null hypothesis, 𝓋₀ = 0.

        Estimates the parameters 𝛂, 𝓋₁, ρ₁, and 𝓋₂ of:

            𝐲 ~ 𝓝(W𝛂, 𝓋₁(ρ₁𝙴𝙴ᵀ + (1-ρ₁)𝙺) + 𝓋₂𝙸),

        using the restricted maximum likelihood approach.
        """
        best = {"lml": -inf, "lmm": None, "rho1": -1.0}
        for rho1, halfSigma in self._halfSigma.items():
            # for rho1, Sigma_qs in self._Sigma_qs.items():
            # Sigma_qs = self._Sigma_qs[rho1]
            # lmm2 = LMM(self._y, self._W, Sigma_qs, restricted=True)
            # lmm2.fit(verbose=False)
            lmm = Kron2Sum(
                self._y[:, newaxis], [[1]], self._W, halfSigma, restricted=True
            )
            lmm.fit(verbose=True)
            lml = lmm.lml()
            if lml > best["lml"]:
                best["lml"] = lml
                best["lmm"] = lmm
                best["rho1"] = rho1

        rho1 = best["rho1"]
        qscov = QSCov(
            self._Sigma_qs[rho1][0][0],
            self._Sigma_qs[rho1][1],
            best["lmm"].C0[0, 0],
            best["lmm"].C1[0, 0],
        )

        self._null_lmm_assoc = {
            "lml": best["lml"],
            "alpha": best["lmm"].beta,
            "v1": best["lmm"].C0[0, 0],
            "rho1": best["rho1"],
            "v2": best["lmm"].C1[0, 0],
            "qscov": qscov,
        }

    def predict_interaction(self, G):
        G = asarray(G, float)
        Y = self._y[:, newaxis]
        E = self._E
        W = self._W
        n_snps = G.shape[1]
        beta_stars = []
        for i in range(n_snps):
            g = G[:, [i]]
            # mean(𝐲) = W𝛂 + 𝐠𝛽₁ + 𝙴𝝲 = 𝙼𝛃
            M = concatenate((W, g, E), axis=1)
            gE = g * E
            best = {"lml": -inf, "rho1": 0}
            hSigma_p = {}
            for rho1 in self._rho1:
                # Σₚ = ρ₁(𝐠⊙𝙴)(𝐠⊙𝙴)ᵀ + (1-ρ₁)𝙺
                hSigma_p[rho1] = concatenate(
                    (sqrt(rho1) * gE, sqrt(1 - rho1) * self._G), axis=1
                )
                # cov(𝐲) = 𝓋₁Σₚ + 𝓋₂𝙸
                lmm = Kron2Sum(Y, [[1]], M, hSigma_p[rho1], restricted=True)
                lmm.fit(verbose=False)
                if lmm.lml() > best["lml"]:
                    best["lml"] = lmm.lml()
                    best["rho1"] = rho1
                    best["lmm"] = lmm

            lmm = best["lmm"]
            # yadj = 𝐲 - 𝙼𝛃
            yadj = self._y - lmm.mean()
            rho1 = best["rho1"]
            v1 = lmm.C0[0, 0]
            v2 = lmm.C1[0, 0]
            # beta_g = 𝛽₁
            beta_g = lmm.beta[W.shape[1]]
            hSigma_p_qs = economic_qs_linear(hSigma_p[rho1], return_q1=False)
            qscov = QSCov(hSigma_p_qs[0][0], hSigma_p_qs[1], v1, v2)
            # v = cov(𝐲)⁻¹(𝐲 - 𝙼𝛃)
            v = qscov.solve(yadj)
            Estar = vstack([E, E])
            sig2_ge = v1 * rho1
            beta_star = beta_g + sig2_ge * multi_dot([Estar, gE.T, v])
            beta_stars.append(beta_star)
        return asarray(beta_stars, float).T

    def scan_interaction(
        self, G, idx_E: Optional[any] = None, idx_G: Optional[any] = None
    ):
        """
        𝐲 = W𝛂 + 𝐠𝛽₁ + 𝐠⊙𝛃₂ + 𝐞 + 𝐮 + 𝛆
           [fixed=X]   [H1]

        𝛃₂ ~ 𝓝(𝟎, 𝓋₃𝙴𝙴ᵀ),
        𝐞 ~ 𝓝(𝟎, 𝓋₁ρ₁𝙴𝙴ᵀ),
        𝐮 ~ 𝓝(𝟎, 𝓋₁(1-ρ₁)𝙺), and
        𝛆 ~ 𝓝(𝟎, 𝓋₂𝙸).

        𝓗₀: 𝓋₃ = 0
        𝓗₁: 𝓋₃ > 0
        """
        # TODO: make sure G is nxp
        from chiscore import davies_pvalue

        G = asarray(G, float)
        n_snps = G.shape[1]
        pvalues = []
        info = {"rho1": [], "e2": [], "g2": [], "eps2": []}
        from time import time

        start = time()
        for i in tqdm(range(n_snps)):
            g = G[:, [i]]
            X = concatenate((self._W, g), axis=1)
            best = {"lml": -inf, "rho1": 0}
            # Null model fitting: find best (𝛂, 𝛽₁, 𝓋₁, 𝓋₂, ρ₁)
            for rho1 in self._rho1:
                # QS = self._Sigma_qs[rho1]
                start = time()
                # halfSigma = self._halfSigma[rho1]
                # Σ = ρ₁𝙴𝙴ᵀ + (1-ρ₁)𝙺
                # cov(y₀) = 𝓋₁Σ + 𝓋₂I
                QS = self._Sigma_qs[rho1]
                lmm = LMM(self._y, X, QS, restricted=True)
                lmm.fit(verbose=False)
                # print(f"Elapsed: {time() - start}")
                # print(f"lml: {lmm.lml()}")
                if lmm.lml() > best["lml"]:
                    best["lml"] = lmm.lml()
                    best["rho1"] = rho1
                    best["lmm"] = lmm
                # print(f"Elapsed: {time() - start}")
            # print(f"Elapsed: {time() - start}")
            # print(best["lml"])
            # print(best["rho1"])
            lmm = best["lmm"]
            # H1 via score test
            # Let K₀ = e²𝙴𝙴ᵀ + g²𝙺 + 𝜀²I
            # e²=𝓋₁ρ₁
            # g²=𝓋₁(1-ρ₁)
            # 𝜀²=𝓋₂
            # with optimal values 𝓋₁ and 𝓋₂ found above.
            info["rho1"].append(best["rho1"])
            info["e2"].append(lmm.v0 * best["rho1"])
            info["g2"].append(lmm.v0 * (1 - best["rho1"]))
            info["eps2"].append(lmm.v1)
            # QS = economic_decomp( Σ(ρ₁) )
            Q0 = self._Sigma_qs[best["rho1"]][0][0]
            S0 = self._Sigma_qs[best["rho1"]][1]
            # e2 = best["lmm"].v0 * best["rho1"]
            # g2 = best["lmm"].v0 * (1 - best["rho1"])
            # eps2 = best["lmm"].v1
            # EE = self._E @ self._E.T
            # K = self._G @ self._G.T
            # K0 = e2 * EE + g2 * K + eps2 * eye(K.shape[0])
            qscov = QSCov(
                Q0,
                S0,
                lmm.v0,  # 𝓋₁
                lmm.v1,  # 𝓋₂
            )
            # start = time()
            # qscov = QSCov(self._Sigma_qs[best["rho1"]], lmm.C0[0, 0], lmm.C1[0, 0])
            # print(f"Elapsed: {time() - start}")
            # X = concatenate((self._E, g), axis=1)
            X = concatenate((self._W, g), axis=1)

            # Let P₀ = K₀⁻¹ - K₀⁻¹X(XᵀK₀⁻¹X)⁻¹XᵀK₀⁻¹.
            P = PMat(qscov, X)
            # P0 = inv(K0) - inv(K0) @ X @ inv(X.T @ inv(K0) @ X) @ X.T @ inv(K0)

            # P₀𝐲 = K₀⁻¹𝐲 - K₀⁻¹X(XᵀK₀⁻¹X)⁻¹XᵀK₀⁻¹𝐲.

            # Useful for permutation
            if idx_E is None:
                E1 = self._E
            else:
                E1 = self._E[idx_E, :]

            # The covariance matrix of H1 is K = K₀ + 𝓋₃diag(𝐠)⋅𝙴𝙴ᵀ⋅diag(𝐠)
            # We have ∂K/∂𝓋₃ = diag(𝐠)⋅𝙴𝙴ᵀ⋅diag(𝐠)
            # The score test statistics is given by
            # Q = ½𝐲ᵀP₀⋅∂K⋅P₀𝐲
            # start = time()

            # Useful for permutation
            if idx_G is None:
                gtest = g.ravel()
            else:
                gtest = g.ravel()[idx_G]

            ss = ScoreStatistic(P, qscov, ddot(gtest, E1))
            Q = ss.statistic(self._y)
            # import numpy as np

            # deltaK = np.diag(gtest) @ EE @ np.diag(gtest)
            # Q_ = 0.5 * self._y.T @ P0 @ deltaK @ P0 @ self._y
            # print(f"Elapsed: {time() - start}")
            # Q is the score statistic for our interaction test and follows a linear
            # combination
            # of chi-squared (df=1) distributions:
            # Q ∼ ∑λχ², where λᵢ are the non-zero eigenvalues of ½√P₀⋅∂K⋅√P₀.
            # Since eigenvals(𝙰𝙰ᵀ) = eigenvals(𝙰ᵀ𝙰) (TODO: find citation),
            # we can compute ½(√∂K)P₀(√∂K) instead.
            # start = time()
            # import scipy as sp
            # sqrtm = sp.linalg.sqrtm
            # np.linalg.eigvalsh(0.5 * sqrtm(P0) @ deltaK @ sqrtm(P0))
            # np.linalg.eigvalsh(0.5 * sqrtm(deltaK) @ P0 @ sqrtm(deltaK))
            # TODO: compare with Liu approximation, maybe try a computational intensive
            # method
            pval, pinfo = davies_pvalue(Q, ss.matrix_for_dist_weights(), True)
            pvalues.append(pval)
            # print(f"Elapsed: {time() - start}")

        info = {key: asarray(v, float) for key, v in info.items()}
        return asarray(pvalues, float), info
