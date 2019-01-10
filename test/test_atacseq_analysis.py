#!/usr/bin/env python

import pytest
import os
import yaml
from .data_generator import generate_project
from peppy import Project
from ngs_toolkit.atacseq import ATACSeqAnalysis
import pybedtools
import pandas as pd
import numpy as np


@pytest.fixture
def get_test_analysis(tmp_path):
    tmp_path = str(tmp_path)  # for Python2

    # Let's make several "reallish" test projects
    to_test = list()
    project_prefix_name = "test-project"
    data_type = "ATAC-seq"
    genome_assemblies = [("human", "hg19"), ("human", "hg38"), ("mouse", "mm10")]

    n_factors = [1, 2, 3][0]
    n_variables = [100, 1000, 10000][0]
    n_replicates = [1, 2, 5][0]
    for organism, genome_assembly in genome_assemblies:
        project_name = "{}_{}_{}_{}_{}_{}".format(
            project_prefix_name, data_type, genome_assembly,
            n_factors, n_variables, n_replicates)

        generate_project(
            output_dir=tmp_path,
            project_name=project_name,
            organism=organism, genome_assembly=genome_assembly,
            data_type=data_type,
            n_factors=n_factors, n_replicates=n_replicates, n_variables=n_variables)

        # first edit the defaul path to the annotation sheet
        config = os.path.join(
            tmp_path, project_name, "metadata", "project_config.yaml")
        c = yaml.safe_load(open(config, 'r'))
        c['metadata']['sample_annotation'] = os.path.abspath(
            os.path.join(tmp_path, project_name, "metadata", "annotation.csv"))
        c['metadata']['comparison_table'] = os.path.abspath(
            os.path.join(tmp_path, project_name, "metadata", "comparison_table.csv"))
        yaml.safe_dump(c, open(config, "w"))

        prj_path = os.path.join(tmp_path, project_name)
        os.chdir(prj_path)

        # project and associated analysis
        analysis = ATACSeqAnalysis(
            name=project_name,
            prj=Project(config),
            results_dir=os.path.join(prj_path, "results"))
        analysis.load_data()

        to_test.append(analysis)
    return to_test


@pytest.fixture
def get_chrom_file():
    from ngs_toolkit.general import download_gzip_file
    url = (
        "https://egg2.wustl.edu/roadmap/data/byFileType/" +
        "chromhmmSegmentations/ChmmModels/coreMarks/jointModel/" +
        "final/E002_15_coreMarks_hg38lift_dense.bed.gz")
    chrom_state_file = os.path.abspath("E002_15_coreMarks_hg38lift_dense.bed")
    download_gzip_file(url, chrom_state_file)
    return chrom_state_file


def test_get_consensus_sites(get_test_analysis):
    import pytest
    for analysis in get_test_analysis:
        with pytest.raises(IOError):
            analysis.get_consensus_sites()


def test_get_supported_peaks(get_test_analysis):
    for analysis in get_test_analysis:
        analysis.support = pd.DataFrame(
            np.random.binomial(1, 0.4, size=analysis.coverage.shape),
            index=analysis.coverage.index,
            columns=analysis.coverage.columns)
        fs = analysis.get_supported_peaks(samples=analysis.samples[:2])
        assert fs.sum() < analysis.coverage.shape[0]


def test_measure_coverage(get_test_analysis):
    import pytest
    for analysis in get_test_analysis:
        with pytest.raises(IOError):
            analysis.measure_coverage()


def test_consensus_set_loading(get_test_analysis):
    for analysis in get_test_analysis:
        assert hasattr(analysis, "sites")
        assert isinstance(analysis.sites, pybedtools.BedTool)


def test_coverage_matrix_loading(get_test_analysis):
    for analysis in get_test_analysis:
        assert hasattr(analysis, "coverage")
        assert isinstance(analysis.coverage, pd.DataFrame)
        assert analysis.coverage.dtypes.all() == int


def test_set_consensus_set(get_test_analysis):
    for analysis in get_test_analysis:
        peaks = os.path.join(analysis.results_dir, analysis.name + "_peak_set.bed")
        analysis.set_consensus_sites(peaks)
        assert hasattr(analysis, "sites")
        sites = pd.read_csv(peaks, header=None)
        assert len(analysis.sites) == sites.shape[0]


def test_rpm_normalization(get_test_analysis):
    for analysis in get_test_analysis:
        qnorm = analysis.normalize_coverage_rpm(save=False)
        assert qnorm.dtypes.all() == np.float
        rpm_file = os.path.join(analysis.results_dir, analysis.name + "_peaks.coverage_rpm.csv")
        assert not os.path.exists(rpm_file)
        qnorm = analysis.normalize_coverage_rpm(save=True)
        assert os.path.exists(rpm_file)
        assert os.stat(rpm_file).st_size > 0


def test_quantile_normalization(get_test_analysis):
    for analysis in get_test_analysis:
        qnorm_p = analysis.normalize_coverage_quantiles(implementation="Python", save=False)
        qnorm_r = analysis.normalize_coverage_quantiles(implementation="R", save=False)

        import scipy
        cors = list()
        for col in qnorm_p.columns:
            cors.append(scipy.stats.pearsonr(qnorm_p[col], qnorm_r[col])[0])
        assert all(np.array(cors) > 0.99)


@pytest.mark.skipif('TRAVIS' in os.environ,
                    reason="Often causes memory error in Travis")
def test_cqn_normalization(get_test_analysis):
    # Test just one for speed
    analysis = [a for a in get_test_analysis if a.genome == "hg38"][0]

    qnorm = analysis.normalize_gc_content()
    assert qnorm.dtypes.all() == np.float
    file = os.path.join(analysis.results_dir, analysis.name + "_peaks.coverage_gc_corrected.csv")
    assert os.path.exists(file)
    assert os.stat(file).st_size > 0


def test_normalize(get_test_analysis):
    for analysis in get_test_analysis:
        qnorm = analysis.normalize_coverage_rpm(save=False)
        assert isinstance(qnorm, pd.DataFrame)
        assert hasattr(analysis, "coverage_rpm")
        qnorm_d = analysis.normalize(method="total", save=False)
        assert np.array_equal(qnorm_d, qnorm)
        qnorm = analysis.normalize_coverage_quantiles(save=False)
        assert hasattr(analysis, "coverage_rpm")
        qnorm_d = analysis.normalize(method="quantile", save=False)
        assert isinstance(qnorm_d, pd.DataFrame)
        assert hasattr(analysis, "coverage_qnorm")
        assert np.array_equal(qnorm_d, qnorm)
        if 'TRAVIS' not in os.environ:
            qnorm = analysis.normalize_gc_content(save=False)
            assert isinstance(qnorm, pd.DataFrame)
            assert hasattr(analysis, "coverage_gc_corrected")


def test_get_matrix_stats(get_test_analysis):
    for analysis in get_test_analysis:
        annot = analysis.get_matrix_stats(quant_matrix='coverage')
        output = os.path.join(
            analysis.results_dir, "{}_peaks.stats_per_region.csv".format(analysis.name))
        assert os.path.exists(output)
        assert os.stat(output).st_size > 0
        assert isinstance(annot, pd.DataFrame)
        cols = ['mean', 'variance', 'std_deviation', 'dispersion', 'qv2', 'amplitude']
        assert all([x in annot.columns.tolist() for x in cols])


def test_get_peak_gene_annotation(get_test_analysis):
    mapping = {"hg19": "grch37", "hg38": "grch38", "mm10": "grcm38"}

    for analysis in get_test_analysis:
        os.chdir(os.path.join(analysis.results_dir, os.pardir))
        annot = analysis.get_peak_gene_annotation(max_dist=1e10)
        tss = os.path.join("reference",
                           "{}.{}.gene_annotation.protein_coding.tss.bed"
                           .format(analysis.organism, mapping[analysis.genome]))
        assert os.path.exists(tss)
        assert os.stat(tss).st_size > 0
        assert isinstance(annot, pd.DataFrame)
        assert annot.shape[0] >= len(analysis.sites)


@pytest.mark.skipif('TRAVIS' in os.environ,
                    reason="Often fails due to API call in Travis")
def test_get_peak_genomic_location(get_test_analysis):
    # Test only one for speed
    analysis = [a for a in get_test_analysis if a.genome == "hg38"][0]
    prefix = os.path.join(
        analysis.results_dir, "..", "reference", "{}.{}.genomic_context")
    fs = [prefix + a for a in [
        ".bed", ".exon.bed", ".genebody.bed", ".intergenic.bed",
        ".intron.bed", ".promoter.bed", ".utr3.bed", ".utr5.bed"]]

    os.chdir(os.path.join(analysis.results_dir, os.pardir))
    annot = analysis.get_peak_genomic_location()

    # check annotation files are produced
    mapping = {"hg19": "grch37", "hg38": "grch38", "mm10": "grcm38"}
    for f in fs:
        f = f.format(analysis.organism, mapping[analysis.genome])
        assert os.path.exists(f)
        assert os.stat(f).st_size > 0

    assert isinstance(annot, pd.DataFrame)
    assert annot.shape[0] >= len(analysis.sites)


def test_peak_chromatin_state(get_test_analysis, get_chrom_file):

    # Can only test hg38
    analysis = [a for a in get_test_analysis if a.genome == "hg38"][0]

    prefix = os.path.join(analysis.results_dir, analysis.name)
    fs = [
        prefix + "_peaks.chrom_state_annotation.csv",
        prefix + "_peaks.chrom_state_annotation_mapping.csv",
        prefix + "_peaks.chrom_state_annotation_background.csv",
        prefix + "_peaks.chrom_state_annotation_background_mapping.csv"]

    attrs = [
        "chrom_state_annotation", "chrom_state_annotation_b",
        "chrom_state_annotation_mapping", "chrom_state_annotation_b_mapping"]

    annot = analysis.get_peak_chromatin_state(chrom_state_file=get_chrom_file)
    assert isinstance(annot, pd.DataFrame)
    assert annot.shape[0] >= len(analysis.sites)

    for f in fs:
        assert os.path.exists(f)
        assert os.stat(f).st_size > 0
    for attr in attrs:
        assert hasattr(analysis, attr)


def test_annotate(get_test_analysis, get_chrom_file):
    analysis = [a for a in get_test_analysis if a.genome == "hg38"][0]

    analysis.get_peak_chromatin_state(chrom_state_file=get_chrom_file)
    analysis.get_matrix_stats(quant_matrix='coverage')
    analysis.get_peak_gene_annotation(max_dist=1e10)
    if 'TRAVIS' not in os.environ:
        # "Often fails due to API call in Travis"
        analysis.get_peak_genomic_location()
    analysis.annotate(quant_matrix="coverage")
    f = os.path.join(
        analysis.results_dir, analysis.name + "_peaks.coverage_qnorm.annotated.csv")
    assert hasattr(analysis, "coverage_annotated")
    assert os.path.exists(f)
    assert os.stat(f).st_size > 0

    cols = [
        'gene_name', 'strand', 'distance',  # from gene_annotation
        'genomic_region',  # from genomic_location
        'chromatin_state',  # from chromatin_state
        'mean', 'variance', 'std_deviation', 'dispersion', 'qv2', 'amplitude', 'iqr']  # from stats

    assert all([c in analysis.coverage_annotated.columns.tolist() for c in cols])


def test_plot_raw_coverage(get_test_analysis):
    for analysis in get_test_analysis:
        analysis.plot_raw_coverage()
        output = os.path.join(analysis.results_dir, analysis.name + ".raw_counts.violinplot.svg")
        assert os.path.exists(output)
        assert os.stat(output).st_size > 0

        attr = "a"
        analysis.plot_raw_coverage(by_attribute=attr)
        output = os.path.join(analysis.results_dir, analysis.name + ".raw_counts.violinplot.by_{}.svg".format(attr))
        assert os.path.exists(output)
        assert os.stat(output).st_size > 0
