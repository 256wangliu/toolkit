#!/usr/bin/env python

"""
A module dedicated to the generation of Analysis, Projects and their data.
"""

import os
import string
import tempfile

from ngs_toolkit.general import query_biomart
from ngs_toolkit.utils import location_index_to_bed
from ngs_toolkit.project_manager import create_project
import numpy as np
import pandas as pd
import pybedtools
import yaml


def generate_count_matrix(
        n_factors=1,
        n_replicates=4,
        n_features=1000,
        intercept_mean=4,
        intercept_std=2,
        coefficient_stds=0.4,
        size_factors=None,
        size_factors_std=0.1,
        dispersion_function=None
):
    import patsy

    if isinstance(coefficient_stds, (int, float)):
        coefficient_stds = [coefficient_stds] * n_factors

    if dispersion_function is None:
        def dispersion_function(x):
            return 4 / x + .1

    # Build sample vs factors table
    dcat = pd.DataFrame(
        patsy.demo_data(
            *(list(string.ascii_lowercase[:n_factors]))))
    dcat.columns = dcat.columns.str.upper()
    for col in dcat.columns:
        dcat[col] = dcat[col].str.upper()
    if n_replicates > 1:
        dcat = pd.concat(
            [dcat for _ in range(int(np.ceil(n_replicates / 2)))]
        ).sort_values(dcat.columns.tolist()).reset_index(drop=True)
    dcat.index = ["S{}_{}".format(i + 1, dcat.loc[i, :].sum()) for i in dcat.index]
    m_samples = dcat.shape[0]

    # make model design table
    design = np.asarray(
        patsy.dmatrix("~ 1 + " + " + ".join(string.ascii_uppercase[:n_factors]), dcat))

    # get 
    beta = np.asarray(
        [np.random.normal(intercept_mean, intercept_std, n_features)] +
        [np.random.normal(0, std, n_features) for std in coefficient_stds]).T

    if size_factors is None:
        size_factors = np.random.normal(1, size_factors_std, (m_samples, 1))

    mean = (2 ** (design @ beta.T) * size_factors).T

    # now sample counts
    dispersion = (1 / dispersion_function(2 ** (beta[:, 1:]))).mean(1).reshape(-1, 1)
    dnum = pd.DataFrame(
        np.random.negative_binomial(
            n=mean, p=dispersion, size=mean.shape),
        columns=dcat.index)
    dcat.index.name = dnum.columns.name = "sample_name"
    return dnum, dcat


def generate_data(
        n_factors=1,
        n_replicates=4,
        n_features=1000,
        coefficient_stds=0.4,
        data_type="ATAC-seq",
        genome_assembly="hg38",
        **kwargs):
    """
    Creates real-looking data

    Parameters
    ----------
    n_factors : :obj:`int`, optional
        Number of factors influencing variance between groups.
        For each factor there will be two groups of samples.

        Defaults to 1.
    n_replicates : :obj:`int`, optional
        Number of replicates per group.

        Defaults to 4.
    n_features : :obj:`int`, optional
        Number of features (i.e. genes, regions) in matrix.

        Defaults to 1000.
    coefficient_stds : {:obj:`int`, :obj:`list`}, optional
        Standard deviation of the coefficients between groups.
        If a list, must match the number of ``n_factors``.

        Defaults to 1.
    data_type : :obj:`bool`, optional
        Data type of the project. Must be one of the ``ngs_toolkit`` classes.

        Default is "ATAC-seq"
    genome_assembly : :obj:`bool`, optional
        Genome assembly of the project.

        Default is "hg38"
    **kwargs : :obj:`dict`
        Additional keyword arguments will be passed to the
        makeExampleDESeqDataSet function of DESeq2.

    Returns
    -------
    :obj:`tuple`
        A tuple of :class:`pandas.DataFrame` objects with numeric
        and categorical data respectively.
    """
    dnum, dcat = generate_count_matrix(
        n_factors=n_factors,
        n_features=n_features,
        coefficient_stds=coefficient_stds,
        **kwargs)

    # add random location indexes
    if data_type in ["ATAC-seq", "ChIP-seq"]:
        dnum.index = get_random_genomic_locations(
            n_features, genome_assembly=genome_assembly
        )
    if data_type in ["RNA-seq"]:
        dnum.index = get_random_genes(
            n_features, genome_assembly=genome_assembly
        )
    if data_type in ["CNV"]:
        from ngs_toolkit.utils import z_score

        dnum.index = get_genomic_bins(
            n_features, genome_assembly=genome_assembly
        )
        dnum = z_score(dnum)

    return dnum, dcat


def generate_project(
        output_dir=None,
        project_name="test_project",
        organism="human",
        genome_assembly="hg38",
        data_type="ATAC-seq",
        n_factors=1,
        only_metadata=False,
        sample_input_files=False,
        initialize=True,
        **kwargs):
    """
    Creates a real-looking PEP-based project with respective input files
    and quantification matrix.

    Parameters
    ----------
    output_dir : :obj:`str`, optional
        Directory to write files to.

        Defaults to a temporary location in the user's ``${TMPDIR}``.
    project_name : :obj:`bool`, optional
        Name for the project.

        Default is "test_project".
    organism : :obj:`bool`, optional
        Organism of the project.

        Default is "human"
    genome_assembly : :obj:`bool`, optional
        Genome assembly of the project.

        Default is "hg38"
    data_type : :obj:`bool`, optional
        Data type of the project. Must be one of the ``ngs_toolkit`` classes.

        Default is "ATAC-seq"
    only_metadata : obj:`bool`, optional
        Whether to only generate metadata for the project or
        input files in addition.

        Default is :obj:`False`.
    sample_input_files : obj:`bool`, optional
        Whether the input files for the respective data type should be produced.

        This would be BAM and peak files for ATAC-seq or BAM files for RNA-seq.

        Default is :obj:`True`.
    initialize : obj:`bool`, optional
        Whether the project should be initialized into an Analysis object
        for the respective ``data_type`` or simply return the path to a PEP
        configuration file.

        Default is :obj:`True`.
    **kwargs : :obj:`dict`
        Additional keyword arguments will be passed to
        :func:`ngs_toolkit.demo.data_generator.generate_data`.

    Returns
    -------
    {:class:`ngs_toolkit.analysis.Analysis`, :obj:`str`}
        The Analysis object for the project or a path to its PEP configuration
        file.
    """
    from ngs_toolkit import _LOGGER
    if output_dir is None:
        output_dir = tempfile.mkdtemp()

    # Create project with projectmanager
    create_project(
        project_name,
        genome_assemblies={organism: genome_assembly},
        overwrite=True,
        root_projects_dir=output_dir,
    )

    # Generate random data
    dnum, dcat = generate_data(
        n_factors=n_factors,
        genome_assembly=genome_assembly, data_type=data_type,
        **kwargs)

    # add additional sample info
    dcat["protocol"] = data_type
    dcat["organism"] = organism
    # now save it
    dcat.to_csv(os.path.join(output_dir, project_name, "metadata", "annotation.csv"))

    # Make comparison table
    comp_table_file = os.path.join(
        output_dir, project_name, "metadata", "comparison_table.csv"
    )
    ct = pd.DataFrame()
    factors = list(string.ascii_uppercase[: n_factors])
    for factor in factors:
        for side, f in [(1, "2"), (0, "1")]:
            ct2 = dcat.query("{} == '{}'".format(factor, factor + f)).index.to_frame()
            ct2["comparison_side"] = side
            ct2["comparison_name"] = "Factor_" + factor + "_" + "2vs1"
            ct2["sample_group"] = "Factor_" + factor + f
            ct = ct.append(ct2)
    ct["comparison_type"] = "differential"
    ct["data_type"] = data_type
    ct["comparison_genome"] = genome_assembly
    ct.to_csv(comp_table_file, index=False)

    # add the sample_attributes and group_attributes depending on the number of factors
    config_file = os.path.join(
        output_dir, project_name, "metadata", "project_config.yaml")
    config = yaml.safe_load(open(config_file, "r"))
    factors = list(string.ascii_uppercase[: n_factors])
    config["sample_attributes"] = ["sample_name"] + factors
    config["group_attributes"] = factors
    config["metadata"]["comparison_table"] = comp_table_file
    yaml.safe_dump(config, open(config_file, "w"))

    # prepare dirs
    dirs = [os.path.join(output_dir, project_name, "results")]
    for d in dirs:
        if not os.path.exists(d):
            os.makedirs(d)

    if not only_metadata:
        if data_type == "ATAC-seq":
            bed = location_index_to_bed(dnum.index)
            bed.to_csv(
                os.path.join(
                    output_dir, project_name, "results", project_name + ".peak_set.bed"
                ),
                index=False,
                sep="\t",
                header=False,
            )
        dnum.to_csv(
            os.path.join(
                output_dir, project_name, "results", project_name + ".matrix_raw.csv"
            )
        )
    if not initialize:
        return config_file

    prev_level = _LOGGER.getEffectiveLevel()
    _LOGGER.setLevel("ERROR")
    an = initialize_analysis_of_data_type(data_type, config_file)
    an.load_data(permissive=True)
    if sample_input_files:
        generate_sample_input_files(an, dnum)
    _LOGGER.setLevel(prev_level)
    return an


def generate_projects(
        output_path=None,
        project_prefix_name="demo-project",
        data_types=["ATAC-seq", "RNA-seq"],
        organisms=["human", "mouse"],
        genome_assemblies=["hg38", "mm10"],
        n_factors=[1, 2, 5],
        n_features=[100, 1000, 10000],
        n_replicates=[1, 3, 5],
        **kwargs):
    """
    Create a list of Projects given ranges of parameters, which will be passed
    to :func:`ngs_toolkit.demo.data_generator.generate_project`.
    """
    ans = list()
    for data_type in data_types:
        for organism, genome_assembly in zip(organisms, genome_assemblies):
            for factors in n_factors:
                for features in n_features:
                    for replicates in n_replicates:
                        project_name = "_".join(
                            str(x) for x in [
                                project_prefix_name,
                                data_type,
                                genome_assembly,
                                factors,
                                features,
                                replicates])

                        an = generate_project(
                            output_dir=output_path,
                            project_name=project_name,
                            organism=organism,
                            genome_assembly=genome_assembly,
                            data_type=data_type,
                            n_factors=factors,
                            n_replicates=replicates,
                            n_features=features,
                            **kwargs)
                        ans.append(an)
    return ans


def generate_bam_file(count_vector, output_bam, genome_assembly="hg38", chrom_sizes_file=None, index=True):
    """Generate BAM file containing reads matching the counts in a vector of features"""
    s = location_index_to_bed(count_vector.index)

    # get reads per region
    i = [i for i, c in count_vector.iteritems() for _ in range(c)]
    s = s.reindex(i)

    # shorten/enlarge by a random fraction; name reads
    d = s['end'] - s['start']
    s = s.assign(
        start=(s['start'] + d * np.random.uniform(-0.2, 0.2, s.shape[0])).astype(int),
        end=(s['end'] + d * np.random.uniform(-0.2, 0.2, s.shape[0])).astype(int),
        name=["{}_read_{}".format(count_vector.name, i) for i in range(s.shape[0])])

    s = pybedtools.BedTool.from_dataframe(s).truncate_to_chrom(genome=genome_assembly).sort()
    # get a file with chromosome sizes (usually not needed but only for bedToBam)
    if chrom_sizes_file is None:
        chrom_sizes_file = tempfile.NamedTemporaryFile().name
        pybedtools.get_chromsizes_from_ucsc(genome=genome_assembly, saveas=chrom_sizes_file)
    s.to_bam(g=chrom_sizes_file).saveas(output_bam)

    if index:
        import pysam
        pysam.index(output_bam)


def generate_peak_file(peak_set, output_peak, genome_assembly="hg38", summits=False):
    """Generate peak files containing regions from a fraction of a given set of features"""
    if not isinstance(peak_set, pybedtools.BedTool):
        peak_set = pybedtools.BedTool(peak_set)

    s = peak_set.to_dataframe()

    # choose a random but non-empty fraction of sites to keep
    while True:
        s2 = s.sample(frac=np.random.uniform())
        if not s2.empty:
            break
    s = pybedtools.BedTool.from_dataframe(s2)
    # shorten/enlarge sites by a random fraction
    s = s.slop(
        l=np.random.uniform(-0.2, 0.2),
        r=np.random.uniform(-0.2, 0.2),
        pct=True, genome=genome_assembly)

    if summits:
        # get middle basepair
        s = s.to_dataframe()
        mid = ((s['end'] - s['start']) / 2).astype(int)
        s.loc[:, 'start'] += mid
        s.loc[:, 'end'] -= mid
        s = pybedtools.BedTool.from_dataframe(s)

    s = s.sort()
    s.saveas(output_peak)


def generate_sample_input_files(analysis, matrix):
    """Generate input files (BAM, peaks) for a sample depending on its data type."""
    if analysis.data_type == "ATAC-seq":
        chrom_sizes_file = tempfile.NamedTemporaryFile().name
        pybedtools.get_chromsizes_from_ucsc(genome=analysis.genome, saveas=chrom_sizes_file)

        if not hasattr(analysis, "sites"):
            analysis.load_data(only_these_keys=['sites'], permissive=True)
        if not hasattr(analysis, "sites"):
            raise AttributeError("Need a consensus peak set to generate sample input files.")

    for sample in analysis.samples:
        if hasattr(sample, "aligned_filtered_bam"):
            if sample.aligned_filtered_bam is not None:
                d = os.path.dirname(sample.aligned_filtered_bam)
                if not os.path.exists(d):
                    os.makedirs(d)
                generate_bam_file(
                    matrix.loc[:, sample.name],
                    sample.aligned_filtered_bam,
                    genome_assembly=analysis.genome,
                    chrom_sizes_file=chrom_sizes_file)
        if hasattr(sample, "peaks"):
            if sample.peaks is not None:
                d = os.path.dirname(sample.peaks)
                if not os.path.exists(d):
                    os.makedirs(d)
                generate_peak_file(
                    analysis.sites, sample.peaks, summits=False,
                    genome_assembly=analysis.genome)
        if hasattr(sample, "summits"):
            if sample.summits is not None:
                d = os.path.dirname(sample.summits)
                if not os.path.exists(d):
                    os.makedirs(d)
                generate_peak_file(
                    analysis.sites, sample.summits, summits=True,
                    genome_assembly=analysis.genome)


def initialize_analysis_of_data_type(data_type, pep_config, *args, **kwargs):
    """Initialize an Analysis object from a PEP config with the appropriate ``data_type``."""
    from ngs_toolkit import ATACSeqAnalysis, ChIPSeqAnalysis, CNVAnalysis, RNASeqAnalysis
    m = {t._data_type: t for t in [ATACSeqAnalysis, ChIPSeqAnalysis, CNVAnalysis, RNASeqAnalysis]}
    return m[data_type](from_pep=pep_config, *args, **kwargs)


def get_random_genomic_locations(
    n_regions, width_mean=500, width_std=400, min_width=300, genome_assembly="hg38"
):
    """Get `n_regions`` number of random genomic locations respecting the boundaries of the ``genome_assembly``"""
    from ngs_toolkit.utils import bed_to_index

    chrom = (pd.Series(["chr"] * n_regions) + pd.Series(np.random.randint(1, 19, n_regions)).astype(str)).tolist()
    start = np.array([0] * n_regions)
    end = np.absolute(np.random.normal(width_mean, width_std, n_regions)).astype(int)
    df = pd.DataFrame([chrom, start.tolist(), end.tolist()]).T
    df.loc[(df[2] - df[1]) < min_width, 2] += min_width
    bed = (
        pybedtools.BedTool.from_dataframe(df)
        .shuffle(genome=genome_assembly)
        .sort()
        .to_dataframe()
    )
    return bed_to_index(bed)


def get_random_genes(n_genes, genome_assembly="hg38"):
    """Get ``n_genes`` number of random genes from the set of genes of the ``genome_assembly``"""

    m = {"hg19": "grch37", "hg38": "grch38", "mm10": "grcm38"}
    o = {"hg19": "hsapiens", "hg38": "hsapiens", "mm10": "mmusculus"}

    g = (
        query_biomart(
            attributes=["external_gene_name"],
            ensembl_version=m[genome_assembly],
            species=o[genome_assembly],
        )
        .squeeze()
        .drop_duplicates()
    )
    return pd.Series(np.random.choice(g, n_genes, replace=False)).sort_values()


def get_genomic_bins(n_bins, distribution="normal", genome_assembly="hg38"):
    """Get a ``size`` number of random genomic bins respecting the boundaries of the ``genome_assembly``"""
    from ngs_toolkit.utils import bed_to_index

    bed = pybedtools.BedTool.from_dataframe(
        pd.DataFrame(dict(pybedtools.chromsizes(genome_assembly))).T.reset_index()
    )
    w = bed.makewindows(
        genome=genome_assembly, w=sum([i.length for i in bed]) / n_bins
    ).to_dataframe()
    return bed_to_index(w.head(n_bins))
