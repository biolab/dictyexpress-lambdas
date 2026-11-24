# Run from cell-de: Rscript fixtures/generate_limma_reference.R
# The saved fixture lets Python tests run without R. Generated with R 4.4.1,
# limma 3.60.6: unweighted paired design, trend=FALSE, robust=FALSE.
library(limma)
library(jsonlite)

make_case <- function(seed, heterogeneous) {
    set.seed(seed)
    k <- 4L
    n <- 80L
    baseline <- runif(n, 500, 3000)
    effect <- c(0.3, -0.3, 2.5, -2.5, 1, -1, rep(0, n - 6))
    noise <- if (heterogeneous) exp(seq(log(0.015), log(1), length.out=n)) else rep(0.1, n)
    reference <- matrix(rep(baseline, each=k), k, n) *
        2^matrix(rnorm(k*n, sd=0.1), k, n)
    residual <- matrix(rnorm(k*n), k, n)
    if (!heterogeneous) residual <- scale(residual)
    selected <- reference * 2^(matrix(rep(effect, each=k), k, n) +
        residual * matrix(rep(noise, each=k), k, n))
    # A never-expressed gene exercises the same filtering and JSON nulls as the service.
    counts <- cbind(round(rbind(selected, reference)), 0)
    libraries <- rowSums(counts)
    log_cpm <- log2((counts + 0.5) / (libraries + 1) * 1e6)
    keep <- colSums(counts / libraries * 1e6 >= 10 / median(libraries) * 1e6) >= k
    replicate <- factor(rep(seq_len(k), 2))
    group <- factor(rep(c("selected", "reference"), each=k), levels=c("reference", "selected"))
    fit <- lmFit(t(log_cpm[, keep]), model.matrix(~ replicate + group))
    coefficient <- "groupselected"
    # Pin the classic empirical-Bayes prior fit and original (worst-case) TREAT
    # when regenerating with versions that also offer newer methods.
    run_test <- function(lfc) {
        fun <- if (lfc == 0) eBayes else treat
        args <- list(fit=fit, trend=FALSE, robust=FALSE)
        if (lfc > 0) args$lfc <- lfc
        if ("legacy" %in% names(formals(fun))) args$legacy <- TRUE
        if ("upshot" %in% names(formals(fun))) args$upshot <- FALSE
        result <- do.call(fun, args)
        p <- result$p.value[, coefficient]
        expand <- function(x) { out <- rep(NA_real_, ncol(counts)); out[keep] <- x; out }
        list(min_log2_fold_change=lfc,
             log2_fold_change=expand(result$coefficients[, coefficient]),
             pvalue=expand(p), fdr=expand(p.adjust(p, method="BH")),
             df_prior=if (is.finite(result$df.prior[1])) result$df.prior[1] else NA_real_)
    }
    list(counts=counts, tests=lapply(c(0, 0.5, 1), run_test))
}

reference <- list(
    R_version=as.character(getRversion()), limma_version=as.character(packageVersion("limma")),
    design="~ replicate + group; selected - reference; unweighted log-CPM",
    cases=list(heterogeneous=make_case(42, TRUE), homogeneous=make_case(43, FALSE)))
write_json(reference, "fixtures/limma_reference.json", digits=NA, pretty=TRUE,
           auto_unbox=TRUE, na="null", matrix="rowmajor")
