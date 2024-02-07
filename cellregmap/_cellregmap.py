from typing import Optional

from glimix_core.lmm import LMM
from numpy import (
    asarray,
    atleast_1d,
    atleast_2d,
    concatenate,
    inf,
    linspace,
    ones,
    sqrt,
    stack,
)
from numpy.linalg import cholesky
from numpy_sugar import ddot
from numpy_sugar.linalg import economic_qs_linear, economic_svd
from tqdm import tqdm

from ._math import PMat, QSCov, ScoreStatistic

import dask.array as da
import xarray as xr
from pandas import DataFrame
from numpy import isnan, logical_not, minimum, nansum
from joblib import Parallel, delayed
import joblib as joblib
import numpy as np
from chiscore import davies_pvalue
from numpy import clip
from numpy_sugar import epsilon
from scipy.stats import chi2

class CellRegMap:
    """
    Mixed-model with genetic effect heterogeneity.

    The CellRegMap model can be cast as:

       𝐲 = W𝛂 + 𝐠𝛽₁ + 𝐠⊙𝛃₂ + 𝐞 + 𝐮 + 𝛆,                                             (1)

    where:

        𝛃₂ ~ 𝓝(𝟎, 𝓋₃𝙴₀𝙴₀ᵀ),
        𝐞 ~ 𝓝(𝟎, 𝓋₁ρ₁𝙴₁𝙴₁ᵀ),
        𝐮 ~ 𝓝(𝟎, 𝓋₁(1-ρ₁)𝙺⊙𝙴₂𝙴₂ᵀ), and
        𝛆 ~ 𝓝(𝟎, 𝓋₂𝙸).

    𝐠⊙𝛃 is a random effect term which models the GxC effect.
    Additionally, W𝛂 models additive covariates and 𝐠𝛽₁ models persistent genetic effects.
    Both are modelled as fixed effects.
    On the other hand, 𝐞, 𝐮 and 𝛆 are modelled as random effects
    𝐞 is the environment effect, 𝐮 is a background term accounting for interactions between population structure
    and environmental structure, and 𝛆 is the iid noise.
    The full covariance of 𝐲 is therefore given by:

        cov(𝐲) = 𝓋₃𝙳𝙴₀𝙴₀ᵀ𝙳 + 𝓋₁ρ₁𝙴₁𝙴₁ᵀ + 𝓋₁(1-ρ₁)𝙺⊙𝙴₂𝙴₂ᵀ + 𝓋₂𝙸,

    where 𝙳 = diag(𝐠). Its marginalised form is given by:

        𝐲 ~ 𝓝(W𝛂 + 𝐠𝛽₁, 𝓋₃𝙳𝙴₀𝙴₀ᵀ𝙳 + 𝓋₁(ρ₁𝙴₁𝙴₁ᵀ + (1-ρ₁)𝙺⊙𝙴₂𝙴₂ᵀ) + 𝓋₂𝙸).

    The CellRegMap method is used to perform an interaction test:

    The interaction test compares the following hypotheses (from Eq. 1):

        𝓗₀: 𝓋₃ = 0
        𝓗₁: 𝓋₃ > 0

    𝓗₀ denotes no GxE effects, while 𝓗₁ models the presence of GxE effects.

    """

    def __init__(self, y, E, W=None, Ls=None, E1=None, hK=None):
        self._y = asarray(y, float).flatten()
        self._E0 = asarray(E, float)
        Ls = [] if Ls is None else Ls

        if W is not None:
            self._W = asarray(W, float) 
        else:
            self._W = ones((self._y.shape[0], 1))

        if E1 is not None:
            self._E1 = asarray(E1, float)
        else:
            self._E1 = asarray(E, float)


        self._Ls = list(asarray(L, float) for L in Ls)

        assert self._W.ndim == 2
        assert self._E0.ndim == 2
        assert self._E1.ndim == 2

        assert self._y.shape[0] == self._W.shape[0]
        assert self._y.shape[0] == self._E0.shape[0]
        assert self._y.shape[0] == self._E1.shape[0]

        for L in Ls:
            assert self._y.shape[0] == L.shape[0]
            assert L.ndim == 2

        self._null_lmm_assoc = {}

        self._halfSigma = {}
        self._Sigma_qs = {}
        # TODO: remove it after debugging
        self._Sigma = {}
        
        # option to set different background (when Ls are defined, background is K*EEt + EEt)
        if len(Ls) == 0:
            # self._rho0 = [1.0]
            if hK is None:   # EEt only as background
                self._rho1 = [1.0]
                self._halfSigma[1.0] = self._E1
                self._Sigma_qs[1.0] = economic_qs_linear(self._E1, return_q1=False)
            else:            # hK is decomposition of K, background in this case is K + EEt
                self._rho1 = linspace(0, 1, 11)
                for rho1 in self._rho1:
                    a = sqrt(rho1)
                    b = sqrt(1 - rho1)
                    hS = concatenate([a * self._E1] + [b * hK], axis=1)
                    self._halfSigma[rho1] = hS
                    self._Sigma_qs[rho1] = economic_qs_linear(
                        self._halfSigma[rho1], return_q1=False
                    )
        else:
            # self._rho0 = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
            self._rho1 = linspace(0, 1, 11)
            for rho1 in self._rho1:
                # Σ = ρ₁𝙴𝙴ᵀ + (1-ρ₁)𝙺⊙E
                # concatenate((sqrt(rho1) * self._E, sqrt(1 - rho1) * G1), axis=1)
                # self._Sigma[rho1] = rho1 * self._EE + (1 - rho1) * self._K
                # self._Sigma_qs[rho1] = economic_qs(self._Sigma[rho1])
                a = sqrt(rho1)
                b = sqrt(1 - rho1)
                hS = concatenate([a * self._E1] + [b * L for L in Ls], axis=1)
                self._halfSigma[rho1] = hS
                self._Sigma_qs[rho1] = economic_qs_linear(
                    self._halfSigma[rho1], return_q1=False
                )

    @property
    def n_samples(self):
        return self._y.shape[0]

    def predict_interaction(self, G, MAF):
        """
        Estimate effect sizes for a given set of SNPs
        """
        # breakpoint()
        G = asarray(G, float)
        E0 = self._E0
        W = self._W
        n_snps = G.shape[1]
        beta_g_s = []
        beta_gxe_s = []

        p = asarray(atleast_1d(MAF), float)
        normalization = 1 / sqrt(2 * p * (1 - p))

        for i in range(n_snps):
            g = G[:, [i]]
            # mean(𝐲) = W𝛂 + 𝐠𝛽₁ + 𝙴𝝲 = 𝙼𝛃
            M = concatenate((W, g, E0), axis=1)
            gE = g * E0
            best = {"lml": -inf, "rho1": 0}
            hSigma_p = {}
            Sigma_qs = {}
            for rho1 in self._rho1:
                # Σ[ρ₁] = ρ₁(𝐠⊙𝙴)(𝐠⊙𝙴)ᵀ + (1-ρ₁)𝙺⊙EEᵀ
                a = sqrt(rho1)
                b = sqrt(1 - rho1)
                hSigma_p[rho1] = concatenate(
                    [a * gE] + [b * L for L in self._Ls], axis=1
                )
                # (
                #     (a * gE, b * self._G), axis=1
                # )
                # cov(𝐲) = 𝓋₁Σ[ρ₁] + 𝓋₂𝙸
                # lmm = Kron2Sum(Y, [[1]], M, hSigma_p[rho1], restricted=True)
                Sigma_qs[rho1] = economic_qs_linear(
                    hSigma_p[rho1], return_q1=False
                )
                lmm = LMM(self._y, M, Sigma_qs[rho1], restricted=True)
                lmm.fit(verbose=False)

                if lmm.lml() > best["lml"]:
                    best["lml"] = lmm.lml()
                    best["rho1"] = rho1
                    best["lmm"] = lmm

            # breakpoint()
            lmm = best["lmm"]
            # beta_g = 𝛽₁
            beta_g = lmm.beta[W.shape[1]]
            # yadj = 𝐲 - 𝙼𝛃
            yadj = (self._y - lmm.mean()).reshape(self._y.shape[0], 1)
            rho1 = best["rho1"]
            v1 = lmm.v0
            v2 = lmm.v1
            hSigma_p_qs = economic_qs_linear(hSigma_p[rho1], return_q1=False)
            qscov = QSCov(hSigma_p_qs[0][0], hSigma_p_qs[1], v1, v2)
            # v = cov(𝐲)⁻¹(𝐲 - 𝙼𝛃)
            v = qscov.solve(yadj)

            sigma2_gxe = v1 * rho1
            beta_gxe = sigma2_gxe * E0 @ (gE.T @ v) * normalization[i]
            # beta_star = (beta_g * normalization + beta_gxe)

            beta_g_s.append(beta_g)
            beta_gxe_s.append(beta_gxe)


        return (asarray(beta_g_s), stack(beta_gxe_s).T)

    def estimate_aggregate_environment(self, g):
        g = atleast_2d(g).reshape((g.size, 1))
        E0 = self._E0
        gE = g * E0
        W = self._W
        M = concatenate((W, g, E0), axis=1)
        best = {"lml": -inf, "rho1": 0}
        hSigma_p = {}
        for rho1 in self._rho1:
            # Σₚ = ρ₁(𝐠⊙𝙴)(𝐠⊙𝙴)ᵀ + (1-ρ₁)𝙺⊙E
            a = sqrt(rho1)
            b = sqrt(1 - rho1)
            hSigma_p[rho1] = concatenate([a * gE] + [b * L for L in self._Ls], axis=1)
            # cov(𝐲) = 𝓋₁Σₚ + 𝓋₂𝙸
            # lmm = Kron2Sum(Y, [[1]], M, hSigma_p[rho1], restricted=True)
            QS = self._Sigma_qs[rho1]
            lmm = LMM(self._y, M, QS, restricted=True)
            lmm.fit(verbose=False)

            if lmm.lml() > best["lml"]:
                best["lml"] = lmm.lml()
                best["rho1"] = rho1
                best["lmm"] = lmm

        lmm = best["lmm"]
        yadj = self._y - lmm.mean()
        # rho1 = best["rho1"]
        v1 = lmm.v0
        v2 = lmm.v1
        rho1 = best["rho1"]
        sigma2_gxe = rho1 * v1
        hSigma_p_qs = economic_qs_linear(hSigma_p[rho1], return_q1=False)
        qscov = QSCov(hSigma_p_qs[0][0], hSigma_p_qs[1], v1, v2)
        # v = cov(𝐲)⁻¹yadj
        v = qscov.solve(yadj)
        beta_gxe = sigma2_gxe * gE.T @ v

        return E0 @ beta_gxe

    def scan_association(self, G):
        info = {"rho1": [], "e2": [], "g2": [], "eps2": []}

        # NULL model
        best = {"lml": -inf, "rho1": 0}
        for rho1 in self._rho1:
            QS = self._Sigma_qs[rho1]
            # LRT for fixed effects requires ML rather than REML estimation
            lmm = LMM(self._y, self._W, QS, restricted=False)
            lmm.fit(verbose=False)

            if lmm.lml() > best["lml"]:
                best["lml"] = lmm.lml()
                best["rho1"] = rho1
                best["lmm"] = lmm

        null_lmm = best["lmm"]
        info["rho1"].append(best["rho1"])
        info["e2"].append(null_lmm.v0 * best["rho1"])
        info["g2"].append(null_lmm.v0 * (1 - best["rho1"]))
        info["eps2"].append(null_lmm.v1)

        n_snps = G.shape[1]
        # QS calculated once before the loop
        QS = self._Sigma_qs[best["rho1"]]

        # Parallel processing for SNP loop
        alt_lmls = Parallel(n_jobs=-1)(delayed(process_snp)(i, G, self._W, self._y, QS) for i in range(n_snps))

        pvalues = lrt_pvalues(null_lmm.lml(), alt_lmls, dof=1)

        info = {key: np.asarray(v, float) for key, v in info.items()}
        return asarray(pvalues, float), info
    
    
    def scan_association_fast(self, G):
        info = {"rho1": [], "e2": [], "g2": [], "eps2": []}

        # NULL model
        best = {"lml": -inf, "rho1": 0}
        for rho1 in self._rho1:
            QS = self._Sigma_qs[rho1]
            # LRT for fixed effects requires ML rather than REML estimation
            lmm = LMM(self._y, self._W, QS, restricted=False)
            lmm.fit(verbose=False)

            if lmm.lml() > best["lml"]:
                best["lml"] = lmm.lml()
                best["rho1"] = rho1
                best["lmm"] = lmm

        null_lmm = best["lmm"]
        info["rho1"].append(best["rho1"])
        info["e2"].append(null_lmm.v0 * best["rho1"])
        info["g2"].append(null_lmm.v0 * (1 - best["rho1"]))
        info["eps2"].append(null_lmm.v1)
        
        # Alternative model 
        lmm = null_lmm
        flmm = lmm.get_fast_scanner()
        alt_lmls = flmm.fast_scan(G, verbose=False)['lml']

        pvalues = lrt_pvalues(null_lmm.lml(), alt_lmls, dof=1)

        info = {key: asarray(v, float) for key, v in info.items()}
        return asarray(pvalues, float), info


    def scan_interaction(
        self, G, idx_E: Optional[any] = None, idx_G: Optional[any] = None
    ):
        """
        𝐲 = W𝛂 + 𝐠𝛽₁ + 𝐠⊙𝛃₂ + 𝐞 + 𝐮 + 𝛆
           [fixed=X]   [H1]

        𝛃₂ ~ 𝓝(𝟎, 𝓋₃𝙴₀𝙴₀ᵀ),
        𝐞 ~ 𝓝(𝟎, 𝓋₁ρ₁𝙴₁𝙴₁ᵀ),
        𝐮 ~ 𝓝(𝟎, 𝓋₁(1-ρ₁)𝙺⊙𝙴₂𝙴₂ᵀ), and
        𝛆 ~ 𝓝(𝟎, 𝓋₂𝙸).

        𝓗₀: 𝓋₃ = 0
        𝓗₁: 𝓋₃ > 0
        """
        # TODO: make sure G is nxp

        G = asarray(G, float)
        n_snps = G.shape[1]

        E0 = self._E0 if idx_E is None else self._E0[idx_E, :]
        
        # Parameters for batch processing
        num_cores = joblib.cpu_count()
        batch_size = max(1, n_snps // num_cores)  # Adjust batch size as needed
        
        # Prepare inputs for the parallel computation
        inputs = [(range(i, min(i + batch_size, n_snps)), G, self._W, E0, self._rho1, self._Sigma_qs, self._y) 
                for i in range(0, n_snps, batch_size)]
        
        results = Parallel(n_jobs=num_cores)(delayed(process_snp_batch)(*inp) for inp in inputs)
        
        # Unpack results
        pvalues = [pval for batch_result in results for pval in batch_result[0]]
        infos = [info for batch_result in results for info in batch_result[1]["rho1"]]
        
        # Post-processing to structure the outputs as desired
        pvalues = np.asarray(pvalues, dtype=float)
        info = {
            "rho1": np.asarray([info_item for batch_result in results for info_item in batch_result[1]["rho1"]], dtype=float),
            "e2": np.asarray([info_item for batch_result in results for info_item in batch_result[1]["e2"]], dtype=float),
            "g2": np.asarray([info_item for batch_result in results for info_item in batch_result[1]["g2"]], dtype=float),
            "eps2": np.asarray([info_item for batch_result in results for info_item in batch_result[1]["eps2"]], dtype=float)
        }
        return pvalues, info

# Helper functions for parallel processing, batch processing (for scan_interaction), and pre-computing null LMMs 
def process_snp(i, G, W, y, QS):
    g = G[:, [i]]
    X = np.concatenate((W, g), axis=1)
    alt_lmm = LMM(y, X, QS, restricted=False)
    alt_lmm.fit(verbose=False)
    return alt_lmm.lml()

def process_snp_batch(snp_indices, G, W, E0, rho1_values, Sigma_qs, y):
    pvalues_batch = []
    info_batch = {"rho1": [], "e2": [], "g2": [], "eps2": []}

    ########## Section needs to be further optimized ##########
    for i in snp_indices:
        g = G[:, [i]].reshape(-1, 1)
        X = np.hstack((W, g))
        best = {"lml": -inf, "rho1": 0, "lmm": None}

        # Null model fitting: find best (𝛂, 𝛽₁, 𝓋₁, 𝓋₂, ρ₁)
        for rho1 in rho1_values:
            QS = Sigma_qs[rho1]
            lmm = LMM(y, X, QS, restricted=True)
            lmm.fit(verbose=False)

            if lmm.lml() > best["lml"]:
                best.update({"lml": lmm.lml(), "rho1": rho1, "lmm": lmm})

        lmm = best["lmm"]
        (Q0,), S0 = Sigma_qs[best["rho1"]]
        qscov = QSCov(Q0, S0, lmm.v0, lmm.v1)
        P = PMat(qscov, X)

        E0 = E0  # Ensure E0 is correctly passed and used
        gtest = g.ravel()  # Ensure gtest is correctly passed and used

        ss = ScoreStatistic(P, qscov, ddot(gtest, E0))
        Q = ss.statistic(y)
        pval, pinfo = davies_pvalue(Q, ss.matrix_for_dist_weights(), True)

        pvalues_batch.append(pval)
        info_batch["rho1"].append(best["rho1"])
        info_batch["e2"].append(lmm.v0 * best["rho1"])
        info_batch["g2"].append(lmm.v0 * (1 - best["rho1"]))
        info_batch["eps2"].append(lmm.v1)
    
    return pvalues_batch, info_batch
    ################### Section ends #######################

def lrt_pvalues(null_lml, alt_lmls, dof=1):
    """
    Compute p-values from likelihood ratios.

    These are likelihood ratio test p-values.

    Parameters
    ----------
    null_lml : float
        Log of the marginal likelihood under the null hypothesis.
    alt_lmls : array_like
        Log of the marginal likelihoods under the alternative hypotheses.
    dof : int
        Degrees of freedom.

    Returns
    -------
    pvalues : ndarray
        P-values.
    """

    lrs = clip(-2 * null_lml + 2 * asarray(alt_lmls, float), epsilon.super_tiny, inf)
    pv = chi2(df=dof).sf(lrs)
    return clip(pv, epsilon.super_tiny, 1 - epsilon.tiny)

def run_association(y, W, E, G, hK=None):
    """
    Association test.
    
    Test for persistent genetic effects.

    Compute p-values using a likelihood ratio test.
    
    Parameters
    ----------
    y : array
        Phenotype
    W : array
	Fixed effect covariates
    E : array
	Cellular contexts
    G : array
	Genotypes (expanded)
    hK : array
	 decompositon of kinship matrix (expanded)
    
    Returns
    -------
    pvalues : ndarray
        P-values.
    """
    if hK is None: hK = None 
    crm = CellRegMap(y, W, E, hK=hK)
    pv = crm.scan_association(G)
    return pv

def run_association_fast(y, W, E, G, hK=None):
    """
    Association test.

    Test for persistent genetic effects.

    Compute p-values using a likelihood ratio test.

    Parameters
    ----------
    y : array
        Phenotype
    W : array
    Fixed effect covariates
    E : array
    Cellular contexts
    G : array
    Genotypes (expanded)
    hK : array
    decompositon of kinship matrix (expanded)

    Returns
    -------
    pvalues : ndarray
        P-values.
    """
    if hK is None: hK = None
    crm = CellRegMap(y, W, E, hK=hK)
    pv = crm.scan_association_fast(G)
    return pv

def get_L_values(hK, E):
    """
    As the definition of Ls is not particulatly intuitive,
    function to extract list of L values given kinship K and 
    cellular environments E
    """
    # get eigendecomposition of EEt
    U, S, _ = economic_svd(E)
    
    # Compute US product (element-wise multiplication with broadcasting)
    # Assuming economic_svd returns S as a 1D array of singular values
    us = U * S[np.newaxis, :]

    # Initialize Ls as an empty list to store the result
    Ls = []

    # Compute Ls using ddot for dot product operations
    # Loop through each column of us and compute the dot product with hK
    for i in range(us.shape[1]):
        Ls.append(ddot(us[:, i], hK))
    
    return Ls

def run_interaction(y, E, G, W=None, E1=None, E2=None, hK=None, idx_G=None):
    """
    Interaction test.

    Test for cell-level genetic effects due to GxC interactions.

    Compute p-values using a score test.

    Parameters
    ----------
    y : array
        Phenotype
    E : array
        Cellular contexts (GxC component)
    G : array
        Genotypes (expanded)
    W : array
        Fixed effect covariates
    hK : array
         decompositon of kinship matrix (expanded)
    E1 : array
        Cellular contexts (C component)
    E2 : array
        Cellular contexts (K*C component)
    idx_G : array
        Permuted genotype index

    Returns
    -------
    pvalues : ndarray
        P-values.
    """
    E1 = E if E1 is None else E1
    E2 = E if E2 is None else E2
    Ls = None if hK is None else get_L_values(hK, E2)
    crm = CellRegMap(y=y, E=E, W=W, E1=E1, Ls=Ls)
    pv = crm.scan_interaction(G, idx_G)
    return pv

def compute_maf(X):
    r"""Compute minor allele frequencies.
    It assumes that ``X`` encodes 0, 1, and 2 representing the number
    of alleles (or dosage), or ``NaN`` to represent missing values.
    Parameters
    ----------
    X : array_like
        Genotype matrix.
    Returns
    -------
    array_like
        Minor allele frequencies.
    Examples
    --------
    .. doctest::
        >>> from numpy.random import RandomState
        >>> from limix.qc import compute_maf
        >>>
        >>> random = RandomState(0)
        >>> X = random.randint(0, 3, size=(100, 10))
        >>>
        >>> print(compute_maf(X)) # doctest: +FLOAT_CMP
        [0.49  0.49  0.445 0.495 0.5   0.45  0.48  0.48  0.47  0.435]
    """
    
    if isinstance(X, da.Array):
        non_missing_count = (X.shape[0] - da.isnan(X).sum(axis=0)).compute()
        allele_sum = da.nansum(X, axis=0).compute()
    elif isinstance(X, DataFrame):
        non_missing_count = logical_not(X.isna()).sum(axis=0)
        allele_sum = X.sum(axis=0, skipna=True)
    elif isinstance(X, xr.DataArray):
        kwargs = {"dim": "sample"} if "sample" in X.dims else {"axis": 0}
        non_missing_count = logical_not(isnan(X)).sum(**kwargs)
        allele_sum = X.sum(skipna=True, **kwargs)
    else:
        non_missing_count = logical_not(isnan(X)).sum(axis=0)
        allele_sum = nansum(X, axis=0)

    # Calculate allele frequency
    freq = allele_sum / (2 * non_missing_count)
    maf = minimum(freq, 1 - freq)
    
    # Set name attribute if present
    if hasattr(maf, "name"):
        maf.name = "maf"

    return maf

def estimate_betas(y, W, E, G, maf=None, E1=None, E2=None, hK=None):
    """
    Effect sizes estimator

    Estimates cell-level genetic effects due to GxC 
    as well as persistent genetic effects across all cells.

    Parameters
    ----------
    y : array
        Phenotype
    W : array
        Fixed effect covariates
    E : array
        Cellular contexts
    G : array
        Genotypes (expanded)
    maf: array
	    Minor allele frequencies (MAFs) for the SNPs in G
    hK : array
         decompositon of kinship matrix (expanded)
    E1 : array
        Cellular contexts (C component)
    E2 : array
        Cellular contexts (K*C component)

    Returns
    -------
    betas : ndarray
        estimated effect sizes, both persistent and due to GxC.
    """
    if E1 is None: E1 = E
    else: E1 = E1
    if E2 is None: E2 = E
    else: E2 = E2
    if hK is None: Ls = None 
    else: Ls = get_L_values(hK, E2)
    crm = CellRegMap(y=y, E=E, W=W, E1=E1, Ls=Ls)
    if maf is None:
        maf = compute_maf(G)
    # print("MAFs: {}".format(maf))
    betas = crm.predict_interaction(G, maf)
    return betas
