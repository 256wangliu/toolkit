#!/usr/bin/env python


import os
from toolkit.general import Analysis, pickle_me
from collections import Counter
import pickle
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pybedtools
import seaborn as sns

# Set settings
pd.set_option("date_dayfirst", True)


class ATACSeqAnalysis(Analysis):
    """
    Class to hold functions and data from analysis.
    """
    def __init__(
            self,
            name="analysis",
            data_dir=os.path.join(".", "data"),
            results_dir=os.path.join(".", "results"),
            pickle_file=None,
            samples=None,
            prj=None,
            from_pickle=False,
            **kwargs):
        super(ATACSeqAnalysis, self).__init__(name, data_dir, results_dir, pickle_file, samples, prj, from_pickle, **kwargs)

    @pickle_me
    def get_consensus_sites(self, samples, region_type="summits", extension=250):
        """Get consensus (union) sites across samples"""
        import re

        for i, sample in enumerate(samples):
            print(sample.name)
            # Get peaks
            try:
                if region_type == "summits":
                    peaks = pybedtools.BedTool(re.sub("_peaks.narrowPeak", "_summits.bed", sample.peaks)).slop(b=extension, genome=sample.genome)
                else:
                    peaks = pybedtools.BedTool(sample.peaks)
            except ValueError:
                print("Peaks for sample {} not found!".format(sample))
                continue
            # Merge overlaping peaks within a sample
            peaks = peaks.merge()
            if i == 0:
                sites = peaks
            else:
                # Concatenate all peaks
                sites = sites.cat(peaks)

        # Merge overlaping peaks across samples
        sites = sites.merge()

        # Filter
        # remove blacklist regions
        blacklist = pybedtools.BedTool(os.path.join(self.data_dir, "external", "wgEncodeDacMapabilityConsensusExcludable.bed"))
        # remove chrM peaks and save
        sites.intersect(v=True, b=blacklist).filter(lambda x: x.chrom != 'chrM').saveas(os.path.join(self.results_dir, self.name + "_peak_set.bed"))

        # Read up again
        self.sites = pybedtools.BedTool(os.path.join(self.results_dir, self.name + "_peak_set.bed"))

    @pickle_me
    def set_consensus_sites(self, bed_file, overwrite=True):
        """Get consensus (union) sites across samples"""
        self.sites = pybedtools.BedTool(bed_file)
        if overwrite:
            self.sites.saveas(os.path.join(self.results_dir, self.name + "_peak_set.bed"))

    @pickle_me
    def calculate_peak_support(self, samples, region_type="peaks"):
        import re

        # calculate support (number of samples overlaping each merged peak)
        for i, sample in enumerate(samples):
            print(sample.name)
            if region_type == "summits":
                peaks = re.sub("_peaks.narrowPeak", "_summits.bed", sample.peaks)
            else:
                peaks = sample.peaks

            if i == 0:
                support = self.sites.intersect(peaks, wa=True, c=True)
            else:
                support = support.intersect(peaks, wa=True, c=True)

        try:
            support = support.to_dataframe()
        except:
            support.saveas("_tmp.peaks.bed")
            support = pd.read_csv("_tmp.peaks.bed", sep="\t", header=None)

        support.columns = ["chrom", "start", "end"] + [sample.name for sample in samples]
        support.to_csv(os.path.join(self.results_dir, self.name + "_peaks.binary_overlap_support.csv"), index=False)

        # get % of total consensus regions found per sample
        # m = pd.melt(support, ["chrom", "start", "end"], var_name="sample_name")
        # groupby
        # n = m.groupby("sample_name").apply(lambda x: len(x[x["value"] == 1]))

        # divide sum (of unique overlaps) by total to get support value between 0 and 1
        support["support"] = support[range(len(samples))].apply(lambda x: sum([i if i <= 1 else 1 for i in x]) / float(len(self.samples)), axis=1)
        # save
        support.to_csv(os.path.join(self.results_dir, self.name + "_peaks.support.csv"), index=False)

        self.support = support

    def get_supported_peaks(self, samples):
        """
        Mask peaks with 0 support in the given samples.
        Returns boolean pd.Series of length `peaks`.
        """
        # calculate support (number of samples overlaping each merged peak)
        return self.support[[s.name for s in samples]].sum(1) != 0

    @pickle_me
    def measure_coverage(self, samples, sites=None, output_file=None):
        import multiprocessing
        import parmap
        from toolkit.general import count_reads_in_intervals

        missing = [s for s in samples if not os.path.exists(s.filtered)]
        if len(missing) > 0:
            print("Samples have missing BAM file: {}".format(missing))
            samples = [s for s in samples if s not in missing]

        if sites is None:
            sites = self.sites
            if output_file is None:
                default = True

        # Count reads with pysam
        # make strings with intervals
        if type(sites) is pybedtools.bedtool.BedTool:
            sites_str = [str(i.chrom) + ":" + str(i.start) + "-" + str(i.stop) for i in self.sites]
        elif type(sites) is pd.core.frame.DataFrame:
            sites_str = (sites.iloc[:, 0] + ":" + sites.iloc[:, 1].astype(str) + "-" + sites.iloc[:, 2].astype(str)).astype(str).tolist()
        elif type(sites) is str:
            sites_str = [str(i.chrom) + ":" + str(i.start) + "-" + str(i.stop) for i in pybedtools.bedtool.BedTool(sites)]

        # count, create dataframe
        coverage = pd.DataFrame(
            map(
                lambda x:
                    pd.Series(x),
                    parmap.map(
                        count_reads_in_intervals,
                        [sample.filtered for sample in samples],
                        sites_str,
                        parallel=True
                    )
            ),
            index=[sample.name for sample in samples]
        ).T

        # Add interval description to df
        ints = map(
            lambda x: (
                x.split(":")[0],
                x.split(":")[1].split("-")[0],
                x.split(":")[1].split("-")[1]
            ),
            coverage.index
        )
        coverage["chrom"] = [x[0] for x in ints]
        coverage["start"] = [int(x[1]) for x in ints]
        coverage["end"] = [int(x[2]) for x in ints]

        # save to disk
        if default:
            self.coverage = coverage
            self.coverage.to_csv(os.path.join(self.results_dir, self.name + "_peaks.raw_coverage.csv"), index=True)
        if output_file is not None:
            coverage.to_csv(output_file, index=True)
        else:
            return coverage

    @pickle_me
    def normalize_coverage_quantiles(self, samples):
        def normalize_quantiles_r(array):
            # install R package
            # source('http://bioconductor.org/biocLite.R')
            # biocLite('preprocessCore')

            import rpy2.robjects as robjects
            import rpy2.robjects.numpy2ri
            rpy2.robjects.numpy2ri.activate()

            robjects.r('require("preprocessCore")')
            normq = robjects.r('normalize.quantiles')
            return np.array(normq(array))

        # Quantifle normalization
        to_norm = self.coverage[[s.name for s in samples]]

        self.coverage_qnorm = pd.DataFrame(
            normalize_quantiles_r(to_norm.values),
            index=to_norm.index,
            columns=to_norm.columns
        )
        self.coverage_qnorm = self.coverage_qnorm.join(self.coverage[['chrom', 'start', 'end']])
        self.coverage_qnorm.to_csv(os.path.join(self.results_dir, self.name + "_peaks.coverage_qnorm.csv"), index=True)

    @pickle_me
    def get_peak_gccontent_length(self, bed_file=None, genome="hg19", fasta_file="/home/arendeiro/resources/genomes/{g}/{g}.fa"):
        """
        Bed file must be a 3-column BED!
        """
        if bed_file is None:
            sites = self.sites
        else:
            sites = pybedtools.BedTool(bed_file)

        nuc = sites.nucleotide_content(fi=fasta_file.format(g=genome)).to_dataframe(comment="#")[["score", "blockStarts"]]
        nuc.columns = ["gc_content", "length"]
        nuc.index = [str(i.chrom) + ":" + str(i.start) + "-" + str(i.stop) for i in sites]

        # get only the sites matching the coverage (not overlapping blacklist)
        self.nuc = nuc.ix[self.coverage.index]

        self.nuc.to_csv(os.path.join(self.results_dir, self.name + "_peaks.gccontent_length.csv"), index=True)

    @pickle_me
    def normalize_gc_content(self, samples):
        """
        # Run manually:
        library("cqn")
        gc = read.csv("results/cll-time_course_peaks.gccontent_length.csv", sep=",", row.names=1)
        cov = read.csv("results/cll-time_course_peaks.coverage_qnorm.csv", sep=",", row.names=1)
        cov2 = cov[, 1:(length(cov) - 3)]

        cqn_out = cqn(cov2, x=gc$gc_content, lengths=gc$length)

        y = cqn_out$y +cqn_out$offset
        y2 = cbind(y, cov[, c("chrom", "start", "end")])
        write.csv(y2, "results/cll-time_course_peaks.coverage_gc_corrected.csv", sep=",")

        # Fix R's stupid colnames replacement
        sed -i 's/ATAC.seq_/ATAC-seq_/g' results/cll-time_course_peaks.coverage_gc_corrected.csv
        """
        def cqn(cov, gc_content, lengths):
            # install R package
            # source('http://bioconductor.org/biocLite.R')
            # biocLite('cqn')
            import rpy2
            rpy2.robjects.numpy2ri.deactivate()

            import rpy2.robjects as robjects
            import rpy2.robjects.pandas2ri
            rpy2.robjects.pandas2ri.activate()

            robjects.r('require("cqn")')
            cqn = robjects.r('cqn')

            cqn_out = cqn(cov, x=gc_content, lengths=lengths)

            y_r = cqn_out[list(cqn_out.names).index('y')]
            y = pd.DataFrame(
                np.array(y_r),
                index=cov.index,
                columns=cov.columns)
            offset_r = cqn_out[list(cqn_out.names).index('offset')]
            offset = pd.DataFrame(
                np.array(offset_r),
                index=cov.index,
                columns=cov.columns)

            return y + offset

        if not hasattr(self, "nuc"):
            self.normalize_coverage_quantiles(samples)

        if not hasattr(self, "nuc"):
            self.get_peak_gccontent_length()

        to_norm = self.coverage_qnorm[[s.name for s in samples]]

        self.coverage_gc_corrected = (
            cqn(cov=to_norm, gc_content=self.nuc["gc_content"], lengths=self.nuc["length"])
            .join(self.coverage[['chrom', 'start', 'end']])
        )

        self.coverage_gc_corrected.to_csv(os.path.join(self.results_dir, self.name + "_peaks.coverage_gc_corrected.csv"), index=True)

    def get_peak_gene_annotation(self):
        """
        Annotates peaks with closest gene.
        Needs files downloaded by prepare_external_files.py
        """
        # create bedtool with hg19 TSS positions
        hg19_refseq_tss = pybedtools.BedTool(os.path.join(self.data_dir, "external", "refseq.refflat.tss.bed"))
        # get closest TSS of each cll peak
        gene_annotation = self.sites.closest(hg19_refseq_tss, d=True).to_dataframe()
        gene_annotation = gene_annotation[['chrom', 'start', 'end'] + gene_annotation.columns[-3:].tolist()]  # TODO: check this
        gene_annotation.columns = ['chrom', 'start', 'end', 'gene_name', "strand", 'distance']

        # aggregate annotation per peak, concatenate various genes (comma-separated)
        self.gene_annotation = gene_annotation.groupby(['chrom', 'start', 'end']).aggregate(lambda x: ",".join(set([str(i) for i in x]))).reset_index()

        # save to disk
        self.gene_annotation.to_csv(os.path.join(self.results_dir, self.name + "_peaks.gene_annotation.csv"), index=False)

        # save distances to all TSSs (for plotting)
        self.closest_tss_distances = gene_annotation['distance'].tolist()
        pickle.dump(self.closest_tss_distances, open(os.path.join(self.results_dir, self.name + "_peaks.closest_tss_distances.pickle"), 'wb'))

    def get_peak_genomic_location(self):
        """
        Annotates peaks with its type of genomic location.
        Needs files downloaded by prepare_external_files.py
        """
        regions = [
            "ensembl_genes.bed", "ensembl_tss2kb.bed",
            "ensembl_utr5.bed", "ensembl_exons.bed", "ensembl_introns.bed", "ensembl_utr3.bed"]

        # create background
        # shuffle regions in genome to create background (keep them in the same chromossome)
        background = self.sites.shuffle(genome='hg19', chrom=True)

        for i, region in enumerate(regions):
            region_name = region.replace(".bed", "").replace("ensembl_", "")
            r = pybedtools.BedTool(os.path.join(self.data_dir, "external", region))
            if region_name == "genes":
                region_name = "intergenic"
                df = self.sites.intersect(r, wa=True, f=0.2, v=True).to_dataframe()
                dfb = background.intersect(r, wa=True, f=0.2, v=True).to_dataframe()
            else:
                df = self.sites.intersect(r, wa=True, u=True, f=0.2).to_dataframe()
                dfb = background.intersect(r, wa=True, u=True, f=0.2).to_dataframe()
            df['genomic_region'] = region_name
            dfb['genomic_region'] = region_name
            if i == 0:
                region_annotation = df
                region_annotation_b = dfb
            else:
                region_annotation = pd.concat([region_annotation, df])
                region_annotation_b = pd.concat([region_annotation_b, dfb])

        # sort
        region_annotation.sort(['chrom', 'start', 'end'], inplace=True)
        region_annotation_b.sort(['chrom', 'start', 'end'], inplace=True)
        # remove duplicates (there shouldn't be anyway)
        region_annotation = region_annotation.reset_index(drop=True).drop_duplicates()
        region_annotation_b = region_annotation_b.reset_index(drop=True).drop_duplicates()
        # join various regions per peak
        self.region_annotation = region_annotation.groupby(['chrom', 'start', 'end']).aggregate(lambda x: ",".join(set([str(i) for i in x]))).reset_index()
        self.region_annotation_b = region_annotation_b.groupby(['chrom', 'start', 'end']).aggregate(lambda x: ",".join(set([str(i) for i in x]))).reset_index()

        # save to disk
        self.region_annotation.to_csv(os.path.join(self.results_dir, self.name + "_peaks.region_annotation.csv"), index=False)
        self.region_annotation_b.to_csv(os.path.join(self.results_dir, self.name + "_peaks.region_annotation_background.csv"), index=False)

    def get_peak_chromatin_state(self):
        """
        Annotates peaks with chromatin states.
        (For now states are from CD19+ cells).
        Needs files downloaded by prepare_external_files.py
        """
        # create bedtool with CD19 chromatin states
        states_cd19 = pybedtools.BedTool(os.path.join(self.data_dir, "external", "E032_15_coreMarks_mnemonics.bed"))

        # create background
        # shuffle regions in genome to create background (keep them in the same chromossome)
        background = self.sites.shuffle(genome='hg19', chrom=True)

        # intersect with cll peaks, to create annotation, get original peaks
        chrom_state_annotation = self.sites.intersect(states_cd19, wa=True, wb=True, f=0.2).to_dataframe()[['chrom', 'start', 'end', 'thickStart']]
        chrom_state_annotation_b = background.intersect(states_cd19, wa=True, wb=True, f=0.2).to_dataframe()[['chrom', 'start', 'end', 'thickStart']]

        # aggregate annotation per peak, concatenate various annotations (comma-separated)
        self.chrom_state_annotation = chrom_state_annotation.groupby(['chrom', 'start', 'end']).aggregate(lambda x: ",".join(x)).reset_index()
        self.chrom_state_annotation.columns = ['chrom', 'start', 'end', 'chromatin_state']

        self.chrom_state_annotation_b = chrom_state_annotation_b.groupby(['chrom', 'start', 'end']).aggregate(lambda x: ",".join(x)).reset_index()
        self.chrom_state_annotation_b.columns = ['chrom', 'start', 'end', 'chromatin_state']

        # save to disk
        self.chrom_state_annotation.to_csv(os.path.join(self.results_dir, self.name + "_peaks.chromatin_state.csv"), index=False)
        self.chrom_state_annotation_b.to_csv(os.path.join(self.results_dir, self.name + "_peaks.chromatin_state_background.csv"), index=False)

    @pickle_me
    def annotate(self, samples, quant_matrix=None):
        if quant_matrix is None:
            quant_matrix = self.coverage_gc_corrected
        # add closest gene
        self.coverage_annotated = pd.merge(
            quant_matrix,
            self.gene_annotation, on=['chrom', 'start', 'end'], how="left")
        # add genomic location
        self.coverage_annotated = pd.merge(
            self.coverage_annotated,
            self.region_annotation[['chrom', 'start', 'end', 'genomic_region']], on=['chrom', 'start', 'end'], how="left")
        # add chromatin state
        self.coverage_annotated = pd.merge(
            self.coverage_annotated,
            self.chrom_state_annotation[['chrom', 'start', 'end', 'chromatin_state']], on=['chrom', 'start', 'end'], how="left")

        # add support
        self.coverage_annotated = pd.merge(
            self.coverage_annotated,
            self.support[['chrom', 'start', 'end', 'support']], on=['chrom', 'start', 'end'], how="left")

        # calculate mean coverage
        self.coverage_annotated['mean'] = self.coverage_annotated[[s.name for s in samples]].mean(axis=1)
        # calculate coverage variance
        self.coverage_annotated['variance'] = self.coverage_annotated[[s.name for s in samples]].var(axis=1)
        # calculate std deviation (sqrt(variance))
        self.coverage_annotated['std_deviation'] = np.sqrt(self.coverage_annotated['variance'])
        # calculate dispersion (variance / mean)
        self.coverage_annotated['dispersion'] = self.coverage_annotated['variance'] / self.coverage_annotated['mean']
        # calculate qv2 (std / mean) ** 2
        self.coverage_annotated['qv2'] = (self.coverage_annotated['std_deviation'] / self.coverage_annotated['mean']) ** 2

        # calculate "amplitude" (max - min)
        self.coverage_annotated['amplitude'] = (
            self.coverage_annotated[[s.name for s in samples]].max(axis=1) -
            self.coverage_annotated[[s.name for s in samples]].min(axis=1)
        )

        # Pair indexes
        assert self.coverage.shape[0] == self.coverage_annotated.shape[0]
        self.coverage_annotated.index = self.coverage.index

        # Save
        self.coverage_annotated.to_csv(os.path.join(self.results_dir, self.name + "_peaks.coverage_qnorm.annotated.csv"), index=True)

    @pickle_me
    def annotate_with_sample_metadata(
            self,
            attributes=[
                "sample_name",
                "patient_id", "timepoint", "cell_type", "compartment", "response",
                "patient_gender", "ighv_mutation_status", "CD38_cells_percentage",
                "del11q", "del13q", "del17p", "tri12", "cll_cells_%", "cell_number", "batch"],
            numerical_attributes=["CD38_cells_percentage", "cll_cells_%", "cell_number"]):

        samples = [s for s in self.samples if s.name in self.coverage_annotated.columns]

        attrs = list()
        for attr in attributes:
            l = list()
            for sample in samples:  # keep order of samples in matrix
                try:
                    l.append(getattr(sample, attr))
                except AttributeError:
                    l.append(np.nan)
            if attr in numerical_attributes:
                l = [float(x) for x in l]
            attrs.append(l)

        # Generate multiindex columns
        index = pd.MultiIndex.from_arrays(attrs, names=attributes)
        self.accessibility = self.coverage_annotated[[s.name for s in samples]]
        self.accessibility.columns = index

        # Save
        self.accessibility.to_csv(os.path.join(self.results_dir, self.name + ".accessibility.annotated_metadata.csv"), index=True)

    def get_level_colors(self, index=None, levels=None, pallete="Paired", cmap="RdBu_r", nan_color=(0.662745, 0.662745, 0.662745, 0.5)):
        if index is None:
            index = self.accessibility.columns

        if levels is not None:
            index = index.droplevel([l.name for l in index.levels if l.name not in levels])

        _cmap = plt.get_cmap(cmap)
        _pallete = plt.get_cmap(pallete)

        colors = list()
        for level in index.levels:
            # determine the type of data in each level
            most_common = Counter([type(x) for x in level]).most_common()[0][0]
            print(level.name, most_common)

            # Add either colors based on categories or numerical scale
            if most_common in [int, float, np.float32, np.float64, np.int32, np.int64]:
                values = index.get_level_values(level.name)
                # Create a range of either 0-100 if only positive values are found
                # or symmetrically from the maximum absolute value found
                if not any(values.dropna() < 0):
                    norm = matplotlib.colors.Normalize(vmin=0, vmax=100)
                else:
                    r = max(abs(values.min()), abs(values.max()))
                    norm = matplotlib.colors.Normalize(vmin=-r, vmax=r)

                col = _cmap(norm(values))
                # replace color for nan cases
                col[np.where(index.get_level_values(level.name).to_series().isnull().tolist())] = nan_color
                colors.append(col.tolist())
            else:
                n = len(set(index.get_level_values(level.name)))
                # get n equidistant colors
                p = [_pallete(1. * i / n) for i in range(n)]
                color_dict = dict(zip(list(set(index.get_level_values(level.name))), p))
                # color for nan cases
                color_dict[np.nan] = nan_color
                col = [color_dict[x] for x in index.get_level_values(level.name)]
                colors.append(col)

        return colors

    def plot_peak_characteristics(self, samples=None):
        def get_sample_reads(bam_file):
            import pysam
            return pysam.AlignmentFile(bam_file).count()

        def get_peak_number(bed_file):
            return len(open(bed_file, "r").read().split("\n"))

        def get_total_open_chromatin(bed_file):
            peaks = pd.read_csv(bed_file, sep="\t", header=None)
            return (peaks.iloc[:, 2] - peaks.iloc[:, 1]).sum()

        def get_peak_lengths(bed_file):
            peaks = pd.read_csv(bed_file, sep="\t", header=None)
            return (peaks.iloc[:, 2] - peaks.iloc[:, 1])

        def get_peak_chroms(bed_file):
            peaks = pd.read_csv(bed_file, sep="\t", header=None)
            return peaks.iloc[:, 0].value_counts()

        # Peaks per sample:
        if samples is None:
            samples = self.samples
        stats = pd.DataFrame([
            map(get_sample_reads, [s.filtered for s in samples]),
            map(get_peak_number, [s.peaks for s in samples]),
            map(get_total_open_chromatin, [s.peaks for s in samples])],
            index=["reads_used", "peak_number", "open_chromatin"],
            columns=[s.name for s in samples]).T

        stats["peaks_norm"] = (stats["peak_number"] / stats["reads_used"]) * 1e3
        stats["open_chromatin_norm"] = (stats["open_chromatin"] / stats["reads_used"])
        stats.to_csv(os.path.join(self.results_dir, "{}.open_chromatin_space.csv".format(self.name)), index=True)
        stats = pd.read_csv(os.path.join(self.results_dir, "{}.open_chromatin_space.csv".format(self.name)), index_col=0)

        stats['knockout'] = stats.index.str.extract("HAP1_(.*?)_")
        stats = stats.ix[stats.index[~stats.index.str.contains("GFP|HAP1_ATAC-seq_WT_Bulk_")]]
        # median lengths per sample (split-apply-combine)
        stats = pd.merge(stats, stats.groupby('knockout')['open_chromatin'].median().to_frame(name='group_open_chromatin').reset_index())
        stats = pd.merge(stats, stats.groupby('knockout')['open_chromatin_norm'].median().to_frame(name='group_open_chromatin_norm').reset_index())

        # plot
        stats = stats.sort_values("open_chromatin_norm")
        fig, axis = plt.subplots(2, 1, figsize=(4 * 2, 6 * 1))
        sns.barplot(x="index", y="open_chromatin", data=stats.reset_index(), palette="summer", ax=axis[0])
        sns.barplot(x="index", y="open_chromatin_norm", data=stats.reset_index(), palette="summer", ax=axis[1])
        axis[0].set_ylabel("Total open chromatin space (bp)")
        axis[1].set_ylabel("Total open chromatin space (normalized)")
        axis[0].set_xticklabels(axis[0].get_xticklabels(), rotation=45, ha="right")
        axis[1].set_xticklabels(axis[1].get_xticklabels(), rotation=45, ha="right")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.total_open_chromatin_space.per_sample.svg".format(self.name)), bbox_inches="tight")

        # per knockout group
        fig, axis = plt.subplots(2, 1, figsize=(4 * 2, 6 * 1))
        stats = stats.sort_values("group_open_chromatin")
        sns.barplot(x="knockout", y="open_chromatin", data=stats.reset_index(), palette="summer", ax=axis[0])
        sns.stripplot(x="knockout", y="open_chromatin", data=stats.reset_index(), palette="summer", ax=axis[0])
        stats = stats.sort_values("group_open_chromatin_norm")
        sns.barplot(x="knockout", y="open_chromatin_norm", data=stats.reset_index(), palette="summer", ax=axis[1])
        sns.stripplot(x="knockout", y="open_chromatin_norm", data=stats.reset_index(), palette="summer", ax=axis[1])
        axis[0].axhline(stats.groupby('knockout')['open_chromatin'].median()["WT"], color="black", linestyle="--")
        axis[1].axhline(stats.groupby('knockout')['open_chromatin_norm'].median()["WT"], color="black", linestyle="--")
        axis[0].set_ylabel("Total open chromatin space (bp)")
        axis[1].set_ylabel("Total open chromatin space (normalized)")
        axis[0].set_xticklabels(axis[0].get_xticklabels(), rotation=45, ha="right")
        axis[1].set_xticklabels(axis[1].get_xticklabels(), rotation=45, ha="right")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.total_open_chromatin_space.per_knockout.svg".format(self.name)), bbox_inches="tight")

        # plot distribution of peak lengths
        sample_peak_lengths = map(get_peak_lengths, [s.peaks for s in samples])
        lengths = pd.melt(pd.DataFrame(sample_peak_lengths, index=[s.name for s in samples]).T, value_name='peak_length', var_name="sample_name").dropna()
        lengths['knockout'] = lengths['sample_name'].str.extract("HAP1_(.*?)_")
        lengths = lengths[~lengths['sample_name'].str.contains("GFP|HAP1_ATAC-seq_WT_Bulk_")]
        # median lengths per sample (split-apply-combine)
        lengths = pd.merge(lengths, lengths.groupby('sample_name')['peak_length'].median().to_frame(name='mean_peak_length').reset_index())
        # median lengths per group (split-apply-combine)
        lengths = pd.merge(lengths, lengths.groupby('knockout')['peak_length'].median().to_frame(name='group_mean_peak_length').reset_index())

        lengths = lengths.sort_values("mean_peak_length")
        fig, axis = plt.subplots(2, 1, figsize=(8 * 1, 4 * 2))
        sns.boxplot(x="sample_name", y="peak_length", data=lengths, palette="summer", ax=axis[0], showfliers=False)
        axis[0].set_ylabel("Peak length (bp)")
        axis[0].set_xticklabels(axis[0].get_xticklabels(), visible=False)
        sns.boxplot(x="sample_name", y="peak_length", data=lengths, palette="summer", ax=axis[1], showfliers=False)
        axis[1].set_yscale("log")
        axis[1].set_ylabel("Peak length (bp)")
        axis[1].set_xticklabels(axis[1].get_xticklabels(), rotation=45, ha="right")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.peak_lengths.per_sample.svg".format(self.name)), bbox_inches="tight")

        lengths = lengths.sort_values("group_mean_peak_length")
        fig, axis = plt.subplots(2, 1, figsize=(8 * 1, 4 * 2))
        sns.boxplot(x="knockout", y="peak_length", data=lengths, palette="summer", ax=axis[0], showfliers=False)
        axis[0].set_ylabel("Peak length (bp)")
        axis[0].set_xticklabels(axis[0].get_xticklabels(), visible=False)
        sns.boxplot(x="knockout", y="peak_length", data=lengths, palette="summer", ax=axis[1], showfliers=False)
        axis[1].set_yscale("log")
        axis[1].set_ylabel("Peak length (bp)")
        axis[1].set_xticklabels(axis[1].get_xticklabels(), rotation=45, ha="right")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.peak_lengths.per_knockout.svg".format(self.name)), bbox_inches="tight")

        # peaks per chromosome per sample
        chroms = pd.DataFrame(map(get_peak_chroms, [s.peaks for s in samples]), index=[s.name for s in samples]).fillna(0).T
        chroms_norm = (chroms / chroms.sum(axis=0)) * 100
        chroms_norm = chroms_norm.ix[["chr{}".format(i) for i in range(1, 23) + ['X', 'Y', 'M']]]

        fig, axis = plt.subplots(1, 1, figsize=(8 * 1, 8 * 1))
        sns.heatmap(chroms_norm, square=True, cmap="summer", ax=axis)
        axis.set_xticklabels(axis.get_xticklabels(), rotation=90, ha="right")
        axis.set_yticklabels(axis.get_yticklabels(), rotation=0, ha="right")
        fig.savefig(os.path.join(self.results_dir, "{}.peak_location.per_sample.svg".format(self.name)), bbox_inches="tight")

        # Peak set across samples:
        # interval lengths
        fig, axis = plt.subplots()
        sns.distplot([interval.length for interval in self.sites if interval.length < 2000], bins=300, kde=False, ax=axis)
        axis.set_xlabel("peak width (bp)")
        axis.set_ylabel("frequency")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.lengths.svg".format(self.name)), bbox_inches="tight")

        # plot support
        fig, axis = plt.subplots()
        sns.distplot(self.support["support"], bins=40, ax=axis)
        axis.set_ylabel("frequency")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.support.svg".format(self.name)), bbox_inches="tight")

        # Plot distance to nearest TSS
        fig, axis = plt.subplots(2, 2, figsize=(4 * 2, 4 * 2), sharex=False, sharey=False)
        sns.distplot([x for x in self.closest_tss_distances if x < 1e5], bins=1000, kde=False, hist=True, ax=axis[0][0])
        sns.distplot([x for x in self.closest_tss_distances if x < 1e5], bins=1000, kde=False, hist=True, ax=axis[0][1])
        sns.distplot([x for x in self.closest_tss_distances if x < 1e6], bins=1000, kde=True, hist=False, ax=axis[1][0])
        sns.distplot([x for x in self.closest_tss_distances if x < 1e6], bins=1000, kde=True, hist=False, ax=axis[1][1])
        for ax in axis.flat:
            ax.set_xlabel("distance to nearest TSS (bp)")
            ax.set_ylabel("frequency")
        axis[0][1].set_yscale("log")
        axis[1][1].set_yscale("log")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.tss_distance.svg".format(self.name)), bbox_inches="tight")

        # Plot genomic regions
        # these are just long lists with genomic regions
        all_region_annotation = self.region_annotation['genomic_region'].apply(lambda x: pd.Series(x.split(","))).stack().reset_index(level=[1], drop=True)
        all_region_annotation.name = "foreground"
        all_region_annotation_b = self.region_annotation_b['genomic_region'].apply(lambda x: pd.Series(x.split(","))).stack().reset_index(level=[1], drop=True)
        all_region_annotation_b.name = "background"

        # count region frequency
        data = all_region_annotation.value_counts().sort_values(ascending=False)
        background = all_region_annotation_b.value_counts().sort_values(ascending=False)
        data = data.to_frame().join(background)
        data["fold_change"] = np.log2(data['foreground'] / data['background'])
        data.index.name = "region"

        # plot also % of genome space "used"
        self.region_annotation["length"] = self.region_annotation["end"] - self.region_annotation["start"]

        s = self.region_annotation.join(all_region_annotation).groupby("foreground")["length"].sum()
        s.name = "size"
        s = (s / pd.Series(self.genome_space)).dropna() * 100
        s.name = "percent_space"
        data = data.join(s)

        # plot together
        g = sns.FacetGrid(data=pd.melt(data.reset_index(), id_vars='region'), col="variable", col_wrap=2, sharex=True, sharey=False, size=4)
        g.map(sns.barplot, "region", "value")
        sns.despine(fig)
        g.savefig(os.path.join(self.results_dir, "{}.genomic_regions.svg".format(self.name)), bbox_inches="tight")

        # # Plot chromatin states
        all_chrom_state_annotation = self.chrom_state_annotation['chromatin_state'].apply(lambda x: pd.Series(x.split(","))).stack().reset_index(level=[1], drop=True)
        all_chrom_state_annotation.name = "foreground"
        all_chrom_state_annotation_b = self.chrom_state_annotation_b['chromatin_state'].apply(lambda x: pd.Series(x.split(","))).stack().reset_index(level=[1], drop=True)
        all_chrom_state_annotation_b.name = "background"

        # count region frequency
        data = all_chrom_state_annotation.value_counts().sort_values(ascending=False)
        background = all_chrom_state_annotation_b.value_counts().sort_values(ascending=False)
        data = data.to_frame().join(background)
        data["fold_change"] = np.log2(data['foreground'] / data['background'])
        data.index.name = "region"

        # plot also % of genome space "used"
        self.chrom_state_annotation["length"] = self.chrom_state_annotation["end"] - self.chrom_state_annotation["start"]

        s = self.chrom_state_annotation.join(all_chrom_state_annotation).groupby("foreground")["length"].sum()
        s.name = "size"
        s = (s / pd.Series(self.genome_space)).dropna() * 100
        s.name = "percent_space"
        data = data.join(s)

        # plot together
        g = sns.FacetGrid(data=pd.melt(data.reset_index(), id_vars='region'), col="variable", col_wrap=2, sharex=True, sharey=False, size=4)
        g.map(sns.barplot, "region", "value")
        sns.despine(fig)
        g.savefig(os.path.join(self.results_dir, "{}.chromatin_states.svg".format(self.name)), bbox_inches="tight")

        # distribution of count attributes
        data = self.coverage_annotated.copy()

        fig, axis = plt.subplots(1)
        sns.distplot(data["mean"], rug=False, ax=axis)
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.mean.distplot.svg".format(self.name)), bbox_inches="tight")

        fig, axis = plt.subplots(1)
        sns.distplot(data["qv2"], rug=False, ax=axis)
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.qv2.distplot.svg".format(self.name)), bbox_inches="tight")

        fig, axis = plt.subplots(1)
        sns.distplot(data["dispersion"], rug=False, ax=axis)
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.dispersion.distplot.svg".format(self.name)), bbox_inches="tight")

        fig, axis = plt.subplots(1)
        sns.distplot(data["support"], rug=False, ax=axis)
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.support.distplot.svg".format(self.name)), bbox_inches="tight")

        # joint
        for metric in ["support", "variance", "std_deviation", "dispersion", "qv2", "amplitude"]:
            p = data[(data["mean"] > 0) & (data[metric] < np.percentile(data[metric], 99) * 3)]
            g = sns.jointplot(p["mean"], p[metric], s=2, alpha=0.2, rasterized=True)
            sns.despine(g.fig)
            g.fig.savefig(os.path.join(self.results_dir, "{}.mean_{}.svg".format(self.name, metric)), bbox_inches="tight", dpi=300)

    def plot_raw_coverage(self, samples, by_attribute=None):
        if by_attribute is None:
            cov = pd.melt(
                np.log2(1 + self.coverage[[s.name for s in samples]]),
                var_name="sample_name", value_name="counts"
            )
            fig, axis = plt.subplots(1, 1, figsize=(6, 1 * 4))
            sns.violinplot("sample_name", "counts", data=cov, ax=axis)
            sns.despine(fig)
            fig.savefig(os.path.join(self.results_dir, self.name + ".raw_counts.violinplot.svg"), bbox_inches="tight")

        else:
            attrs = set([getattr(s, by_attribute) for s in samples])
            fig, axis = plt.subplots(len(attrs), 1, figsize=(8, len(attrs) * 6))
            for i, attr in enumerate(attrs):
                print(attr)
                cov = pd.melt(
                    np.log2(1 + self.coverage[[s.name for s in samples if getattr(s, by_attribute) == attr]]),
                    var_name="sample_name", value_name="counts"
                )
                sns.violinplot("sample_name", "counts", data=cov, ax=axis[i])
                axis[i].set_title(attr)
                axis[i].set_xticklabels(axis[i].get_xticklabels(), rotation=90)
            sns.despine(fig)
            fig.savefig(os.path.join(self.results_dir, self.name + ".raw_counts.violinplot.by_{}.svg".format(by_attribute)), bbox_inches="tight")

    def plot_coverage(self):
        data = self.accessibility.copy()
        # (rewrite to avoid putting them there in the first place)
        variables = ['gene_name', 'genomic_region', 'chromatin_state']

        for variable in variables:
            d = data[variable].str.split(',').apply(pd.Series).stack()  # separate comma-delimited fields
            d.index = d.index.droplevel(1)  # returned a multiindex Series, so get rid of second index level (first is from original row)
            data = data.drop([variable], axis=1)  # drop original column so there are no conflicts
            d.name = variable
            data = data.join(d)  # joins on index

        variables = [
            'chrom', 'start', 'end',
            'ensembl_transcript_id', 'distance', 'ensembl_gene_id', 'support',
            'mean', 'variance', 'std_deviation', 'dispersion', 'qv2',
            'amplitude', 'gene_name', 'genomic_region', 'chromatin_state']
        # Plot
        data_melted = pd.melt(
            data,
            id_vars=variables, var_name="sample", value_name="norm_counts")

        # transform dispersion
        data_melted['dispersion'] = np.log2(1 + data_melted['dispersion'])

        # Together in same violin plot
        fig, axis = plt.subplots(1)
        sns.violinplot("genomic_region", "norm_counts", data=data_melted, ax=axis)
        fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.per_genomic_region.violinplot.svg"), bbox_inches="tight")

        # dispersion
        fig, axis = plt.subplots(1)
        sns.violinplot("genomic_region", "dispersion", data=data_melted, ax=axis)
        fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.dispersion.per_genomic_region.violinplot.svg"), bbox_inches="tight")

        # dispersion
        fig, axis = plt.subplots(1)
        sns.violinplot("genomic_region", "qv2", data=data_melted, ax=axis)
        fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.qv2.per_genomic_region.violinplot.svg"), bbox_inches="tight")

        fig, axis = plt.subplots(1)
        sns.violinplot("chromatin_state", "norm_counts", data=data_melted, ax=axis)
        fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.chromatin_state.violinplot.svg"), bbox_inches="tight")

        fig, axis = plt.subplots(1)
        sns.violinplot("chromatin_state", "dispersion", data=data_melted, ax=axis)
        fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.dispersion.chromatin_state.violinplot.svg"), bbox_inches="tight")

        fig, axis = plt.subplots(1)
        sns.violinplot("chromatin_state", "qv2", data=data_melted, ax=axis)
        fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.qv2.chromatin_state.violinplot.svg"), bbox_inches="tight")

        # separated by variable in one grid
        g = sns.FacetGrid(data_melted, col="genomic_region", col_wrap=3)
        g.map(sns.distplot, "mean", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.mean.per_genomic_region.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="genomic_region", col_wrap=3)
        g.map(sns.distplot, "dispersion", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.dispersion.per_genomic_region.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="genomic_region", col_wrap=3)
        g.map(sns.distplot, "qv2", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.qv2.per_genomic_region.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="genomic_region", col_wrap=3)
        g.map(sns.distplot, "support", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.support.per_genomic_region.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="chromatin_state", col_wrap=3)
        g.map(sns.distplot, "mean", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.mean.chromatin_state.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="chromatin_state", col_wrap=3)
        g.map(sns.distplot, "dispersion", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.dispersion.chromatin_state.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="chromatin_state", col_wrap=3)
        g.map(sns.distplot, "qv2", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.qv2.chromatin_state.distplot.svg"), bbox_inches="tight")

        g = sns.FacetGrid(data_melted, col="chromatin_state", col_wrap=3)
        g.map(sns.distplot, "support", hist=False, rug=False)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts.support.chromatin_state.distplot.svg"), bbox_inches="tight")
        plt.close("all")

    def plot_variance(self, samples):

        g = sns.jointplot('mean', "dispersion", data=self.accessibility, kind="kde")
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts_per_sample.dispersion.svg"), bbox_inches="tight")

        g = sns.jointplot('mean', "qv2", data=self.accessibility)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts_per_sample.qv2_vs_mean.svg"), bbox_inches="tight")

        g = sns.jointplot('support', "qv2", data=self.accessibility)
        g.fig.savefig(os.path.join(self.results_dir, self.name + ".norm_counts_per_sample.support_vs_qv2.svg"), bbox_inches="tight")

        # Filter out regions which the maximum across all samples is below a treshold
        filtered = self.accessibility[self.accessibility[[sample.name for sample in samples]].max(axis=1) > 3]

        sns.jointplot('mean', "dispersion", data=filtered)
        plt.savefig(os.path.join(self.results_dir, self.name + ".norm_counts_per_sample.dispersion.filtered.svg"), bbox_inches="tight")
        plt.close('all')
        sns.jointplot('mean', "qv2", data=filtered)
        plt.savefig(os.path.join(self.results_dir, self.name + ".norm_counts_per_sample.support_vs_qv2.filtered.svg"), bbox_inches="tight")

    def unsupervised(
            self, samples, attributes=[
                "patient_id", "timepoint", "cell_type", "compartment", "response",
                "patient_gender", "ighv_mutation_status", "CD38_cells_percentage",
                "del11q", "del13q", "del17p", "tri12", "cll_cells_%", "cell_number", "batch"],
            exclude=[]):
        """
        """
        from sklearn.decomposition import PCA
        from sklearn.manifold import MDS
        from collections import OrderedDict
        import re
        import itertools
        from scipy.stats import kruskal
        from scipy.stats import pearsonr

        color_dataframe = pd.DataFrame(self.get_level_colors(levels=attributes), index=attributes, columns=[s.name for s in self.samples])

        # exclude samples if needed
        samples = [s for s in samples if s.name not in exclude]
        color_dataframe = color_dataframe[[s.name for s in samples]]
        sample_display_names = color_dataframe.columns.str.replace("ATAC-seq_", "")

        # exclude attributes if needed
        to_plot = attributes[:]
        to_exclude = ["sample_name"]
        for attr in to_exclude:
            try:
                to_plot.pop(to_plot.index(attr))
            except:
                continue

        color_dataframe = color_dataframe.ix[to_plot]

        # All regions
        X = self.accessibility[[s.name for s in samples if s.name not in exclude]]

        # Pairwise correlations
        g = sns.clustermap(
            X.corr(), xticklabels=False, yticklabels=sample_display_names, annot=True,
            cmap="Spectral_r", figsize=(15, 15), cbar_kws={"label": "Pearson correlation"}, row_colors=color_dataframe.values.tolist())
        g.ax_heatmap.set_yticklabels(g.ax_heatmap.get_yticklabels(), rotation=0, fontsize='xx-small')
        g.ax_heatmap.set_xlabel(None, visible=False)
        g.ax_heatmap.set_ylabel(None, visible=False)
        g.fig.savefig(os.path.join(self.results_dir, "{}.all_sites.corr.clustermap.svg".format(self.name)), bbox_inches='tight')

        # MDS
        mds = MDS(n_jobs=-1)
        x_new = mds.fit_transform(X.T)
        # transform again
        x = pd.DataFrame(x_new)
        xx = x.apply(lambda j: (j - j.mean()) / j.std(), axis=0)

        fig, axis = plt.subplots(1, len(to_plot), figsize=(4 * len(to_plot), 4 * 1))
        axis = axis.flatten()
        for i, attr in enumerate(to_plot):
            for j in range(len(xx)):
                try:
                    label = getattr(samples[j], to_plot[i])
                except AttributeError:
                    label = np.nan
                axis[i].scatter(xx.ix[j][0], xx.ix[j][1], s=50, color=color_dataframe.ix[attr][j], label=label)
            axis[i].set_title(to_plot[i])
            axis[i].set_xlabel("MDS 1")
            axis[i].set_ylabel("MDS 2")
            axis[i].set_xticklabels(axis[i].get_xticklabels(), visible=False)
            axis[i].set_yticklabels(axis[i].get_yticklabels(), visible=False)

            # Unique legend labels
            handles, labels = axis[i].get_legend_handles_labels()
            by_label = OrderedDict(zip(labels, handles))
            if any([type(c) in [str, unicode] for c in by_label.keys()]) and len(by_label) <= 20:
                if not any([re.match("^\d", c) for c in by_label.keys()]):
                    axis[i].legend(by_label.values(), by_label.keys())
        fig.savefig(os.path.join(self.results_dir, "{}.all_sites.mds.svg".format(self.name)), bbox_inches="tight")

        # PCA
        pca = PCA()
        x_new = pca.fit_transform(X.T)
        # transform again
        x = pd.DataFrame(x_new)
        xx = x.apply(lambda j: (j - j.mean()) / j.std(), axis=0)

        # plot % explained variance per PC
        fig, axis = plt.subplots(1)
        axis.plot(
            range(1, len(pca.explained_variance_) + 1),  # all PCs
            (pca.explained_variance_ / pca.explained_variance_.sum()) * 100, 'o-')  # % of total variance
        axis.axvline(len(to_plot), linestyle='--')
        axis.set_xlabel("PC")
        axis.set_ylabel("% variance")
        sns.despine(fig)
        fig.savefig(os.path.join(self.results_dir, "{}.all_sites.pca.explained_variance.svg".format(self.name)), bbox_inches='tight')

        # plot
        pcs = min(xx.shape[0] - 1, 8)
        fig, axis = plt.subplots(pcs, len(to_plot), figsize=(4 * len(to_plot), 4 * pcs))
        for pc in range(pcs):
            for i, attr in enumerate(to_plot):
                for j in range(len(xx)):
                    try:
                        label = getattr(samples[j], to_plot[i])
                    except AttributeError:
                        label = np.nan
                    axis[pc, i].scatter(xx.ix[j][pc], xx.ix[j][pc + 1], s=50, color=color_dataframe.ix[attr][j], label=label)
                axis[pc, i].set_title(to_plot[i])
                axis[pc, i].set_xlabel("PC {}".format(pc + 1))
                axis[pc, i].set_ylabel("PC {}".format(pc + 2))
                axis[pc, i].set_xticklabels(axis[pc, i].get_xticklabels(), visible=False)
                axis[pc, i].set_yticklabels(axis[pc, i].get_yticklabels(), visible=False)

                # Unique legend labels
                handles, labels = axis[pc, i].get_legend_handles_labels()
                by_label = OrderedDict(zip(labels, handles))
                if any([type(c) in [str, unicode] for c in by_label.keys()]) and len(by_label) <= 20:
                    # if not any([re.match("^\d", c) for c in by_label.keys()]):
                    axis[pc, i].legend(by_label.values(), by_label.keys())
        fig.savefig(os.path.join(self.results_dir, "{}.all_sites.pca.svg".format(self.name)), bbox_inches="tight")

        # Get PC1 loadings
        # import math
        # loadings = pd.Series(pca.components_[0, :], index=X.index).sort_values()
        # loadings = pca.components_.T * math.sqrt(pca.explained_variance_)

        #

        # # Test association of PCs with attributes
        associations = list()
        for pc in range(pcs):
            for attr in attributes[1:]:
                print("PC {}; Attribute {}.".format(pc + 1, attr))
                sel_samples = [s for s in samples if hasattr(s, attr)]
                sel_samples = [s for s in sel_samples if not pd.isnull(getattr(s, attr))]

                # Get all values of samples for this attr
                groups = set([getattr(s, attr) for s in sel_samples])

                # Determine if attr is categorical or continuous
                if all([type(i) in [str, bool] for i in groups]) or len(groups) == 2:
                    variable_type = "categorical"
                elif all([type(i) in [int, float, np.int64, np.float64] for i in groups]):
                    variable_type = "numerical"
                else:
                    print("attr %s cannot be tested." % attr)
                    associations.append([pc + 1, attr, variable_type, np.nan, np.nan, np.nan])
                    continue

                if variable_type == "categorical":
                    # It categorical, test pairwise combinations of attributes
                    for group1, group2 in itertools.combinations(groups, 2):
                        g1_indexes = [i for i, s in enumerate(sel_samples) if getattr(s, attr) == group1]
                        g2_indexes = [i for i, s in enumerate(sel_samples) if getattr(s, attr) == group2]

                        g1_values = xx.loc[g1_indexes, pc]
                        g2_values = xx.loc[g2_indexes, pc]

                        # Test ANOVA (or Kruskal-Wallis H-test)
                        p = kruskal(g1_values, g2_values)[1]

                        # Append
                        associations.append([pc + 1, attr, variable_type, group1, group2, p])

                elif variable_type == "numerical":
                    # It numerical, calculate pearson correlation
                    indexes = [i for i, s in enumerate(samples) if s in sel_samples]
                    pc_values = xx.loc[indexes, pc]
                    trait_values = [getattr(s, attr) for s in sel_samples]
                    p = pearsonr(pc_values, trait_values)[1]

                    associations.append([pc + 1, attr, variable_type, np.nan, np.nan, p])

        associations = pd.DataFrame(associations, columns=["pc", "attribute", "variable_type", "group_1", "group_2", "p_value"])

        # write
        associations.to_csv(os.path.join(self.results_dir, "{}.all_sites.pca.variable_principle_components_association.csv".format(self.name)), index=False)

        # Plot
        # associations[associations['p_value'] < 0.05].drop(['group_1', 'group_2'], axis=1).drop_duplicates()
        # associations.drop(['group_1', 'group_2'], axis=1).drop_duplicates().pivot(index="pc", columns="attribute", values="p_value")
        pivot = associations.groupby(["pc", "attribute"]).min()['p_value'].reset_index().pivot(index="pc", columns="attribute", values="p_value").dropna(axis=1)

        # heatmap of -log p-values
        g = sns.clustermap(-np.log10(pivot), row_cluster=False, annot=True, cbar_kws={"label": "-log10(p_value) of association"})
        g.ax_heatmap.set_xticklabels(g.ax_heatmap.get_xticklabels(), rotation=45, ha="right")
        g.fig.savefig(os.path.join(self.results_dir, "{}.all_sites.pca.variable_principle_components_association.svg".format(self.name)), bbox_inches="tight")

        # heatmap of masked significant
        g = sns.clustermap((pivot < 0.05).astype(int), row_cluster=False, cbar_kws={"label": "significant association"})
        g.ax_heatmap.set_xticklabels(g.ax_heatmap.get_xticklabels(), rotation=45, ha="right")
        g.fig.savefig(os.path.join(self.results_dir, "{}.all_sites.pca.variable_principle_components_association.masked.svg".format(self.name)), bbox_inches="tight")

        # Each cell type separately
        for cell_type in set([s.cell_type for s in samples]):
            print(cell_type)
            Xt = X.loc[:, X.columns.get_level_values("cell_type") == cell_type]
            sel_samples = [s for s in samples if s.name in Xt.columns.get_level_values("sample_name")]

            to_plot = [q for q in attributes if q != "cell_type"]

            color_dataframe = pd.DataFrame(self.get_level_colors(levels=to_plot, index=Xt.columns), index=to_plot, columns=Xt.columns.get_level_values("sample_name"))
            sample_display_names = color_dataframe.columns.str.replace("ATAC-seq_", "").str.replace("_hg19", "")

            # Pairwise correlations
            g = sns.clustermap(
                Xt.corr(), xticklabels=False, yticklabels=sample_display_names, annot=True,
                cmap="Spectral_r", figsize=(15, 15), cbar_kws={"label": "Pearson correlation"}, row_colors=color_dataframe.values.tolist())
            g.ax_heatmap.set_yticklabels(g.ax_heatmap.get_yticklabels(), rotation=0)
            g.ax_heatmap.set_xlabel(None, visible=False)
            g.ax_heatmap.set_ylabel(None, visible=False)
            g.fig.savefig(os.path.join(self.results_dir, "{}.all_sites.{}.corr.clustermap.svg".format(self.name, cell_type)), bbox_inches='tight')

            # PCA
            pca = PCA()
            x_new = pca.fit_transform(Xt.T)
            # transform again
            x = pd.DataFrame(x_new)
            xx = x.apply(lambda j: (j - j.mean()) / j.std(), axis=0)

            # plot
            pcs = min(xx.shape[0] - 1, 6)
            fig, axis = plt.subplots(pcs, len(to_plot), figsize=(4 * len(to_plot), 4 * pcs))
            for pc in range(pcs):
                for i, attr in enumerate(to_plot):
                    for j in range(len(xx)):
                        try:
                            label = getattr(sel_samples[j], to_plot[i])
                        except AttributeError:
                            label = np.nan
                        axis[pc, i].scatter(xx.ix[j][pc], xx.ix[j][pc + 1], s=50, color=color_dataframe.ix[attr][j], label=label)
                    axis[pc, i].set_title(to_plot[i])
                    axis[pc, i].set_xlabel("PC {}".format(pc + 1))
                    axis[pc, i].set_ylabel("PC {}".format(pc + 2))
                    axis[pc, i].set_xticklabels(axis[pc, i].get_xticklabels(), visible=False)
                    axis[pc, i].set_yticklabels(axis[pc, i].get_yticklabels(), visible=False)

                    # Unique legend labels
                    handles, labels = axis[pc, i].get_legend_handles_labels()
                    by_label = OrderedDict(zip(labels, handles))
                    if any([type(c) in [str, unicode] for c in by_label.keys()]) and len(by_label) <= 20:
                        axis[pc, i].legend(by_label.values(), by_label.keys())
            fig.savefig(os.path.join(self.results_dir, "{}.all_sites.pca.{}.svg".format(self.name, cell_type)), bbox_inches="tight")

    def unsupervised_enrichment(self, samples, variables=["IL10_status", "subset", "replicate", "batch"]):
        """
        """
        from sklearn.decomposition import PCA
        import itertools
        from statsmodels.sandbox.stats.multicomp import multipletests

        def jackstraw(data, pcs, n_vars, n_iter=100):
            """
            """
            import rpy2.robjects as robj
            from rpy2.robjects.vectors import IntVector
            from rpy2.robjects import pandas2ri
            pandas2ri.activate()

            run = robj.r("""
                run = function(data, pcs, n_vars, B) {
                    library(jackstraw)
                    out <- jackstraw.PCA(data, r1=pcs, r=n_vars, B=B)
                    return(out$p.value)
                }
            """)
            # save to disk just in case
            data.to_csv("_tmp_matrix.jackstraw.csv", index=True)

            if type(pcs) is not int:
                pcs = IntVector(pcs)
            return run(data.values, pcs, n_vars, n_iter)

        def lola(wd="~/projects/breg/results/plots/"):
            """
            Performs location overlap analysis (LOLA) on bedfiles with regions sets.
            """
            import rpy2.robjects as robj

            run = robj.r("""
                function(wd) {
                    setwd(wd)

                    library("LOLA")

                    dbPath1 = "/data/groups/lab_bock/shared/resources/regions/LOLACore/mm10/"
                    dbPath2 = "/data/groups/lab_bock/shared/resources/regions/customRegionDB/mm10/"
                    regionDB = loadRegionDB(c(dbPath1, dbPath2))

                    for (topdir in sample(list.dirs(".", recursive=FALSE))){
                        if (!"allEnrichments.txt" %in% list.files(topdir, recursive=FALSE)){
                            print(topdir)
                            userSet <- LOLA::readBed(paste0(basename(topdir), "/", basename(topdir), ".bed"))
                            userUniverse  <- LOLA::readBed(paste0(basename(topdir), "/", "universe_sites.bed"))

                            lolaResults = runLOLA(list(userSet), userUniverse, regionDB, cores=8)
                            writeCombinedEnrichment(lolaResults, outFolder=topdir, includeSplits=FALSE)
                        }
                    }
                }
            """)

            # convert the pandas dataframe to an R dataframe
            run()

        def vertical_line(x, **kwargs):
            plt.axvline(x.mean(), **kwargs)

        # Get accessibility matrix excluding sex chroms
        X = self.coverage_qnorm_annotated[[s.name for s in samples]]
        X = X.ix[X.index[~X.index.str.contains("chrX|chrY")]]

        # Now perform association analysis (jackstraw)
        n_vars = 3
        max_sig = 1000
        alpha = 0.01
        pcs = range(1, n_vars + 1)
        pcs += list(itertools.combinations(pcs, 2))

        p_values = pd.DataFrame(index=X.index)
        for pc in pcs:
            print(pc)
            out = jackstraw(X, pc, n_vars, 100).flatten()
            if type(pc) is int:
                p_values[pc] = out
            else:
                p_values["+".join([str(x) for x in pc])] = out
        q_values = p_values.apply(lambda x: multipletests(x, method="fdr_bh")[1])
        p_values.to_csv(os.path.join(self.results_dir, "PCA.PC_pvalues.csv"), index=True)

        p_values = pd.read_csv(os.path.join(self.results_dir, "PCA.PC_pvalues.csv"), index_col=0)

        # Get enrichments of each PC-regions
        for pc in p_values.columns:
            p = p_values[pc].sort_values()
            sig = p[p < alpha].index

            # Cap to a maximum number of regions
            if len(sig) > max_sig:
                sig = p.head(max_sig).index

            # Run LOLA
            # save to bed
            output_folder = os.path.join(self.results_dir, "PCA.PC{}_regions".format(pc))
            if not os.path.exists(output_folder):
                os.makedirs(output_folder)
            universe_file = os.path.join(output_folder, "universe_sites.bed")
            self.sites.saveas(universe_file)
            bed_file = os.path.join(output_folder, "PCA.PC{}_regions.bed".format(pc))
            self.coverage[['chrom', 'start', 'end']].ix[sig].to_csv(bed_file, sep="\t", header=False, index=False)

        lola("/home/arendeiro/projects/breg/results/plots/")

        lola_enrichments = pd.DataFrame()
        enrichr_enrichments = pd.DataFrame()
        for pc in p_values.columns:
            # # read lola
            # output_folder = os.path.join(self.results_dir, "PCA.PC{}_regions".format(pc))
            # lol = pd.read_csv(os.path.join(output_folder, "allEnrichments.txt"), sep="\t")
            # lol["PC"] = pc
            # lola_enrichments = lola_enrichments.append(lol)

            # Get genes, run enrichr
            p = p_values[pc].sort_values()
            sig = p[p < alpha].index

            # Cap to a maximum number of regions
            if len(sig) > max_sig:
                sig = p.head(max_sig).index

            sig_genes = self.coverage_qnorm_annotated['gene_name'].ix[sig]
            sig_genes = [x for g in sig_genes.dropna().astype(str).tolist() for x in g.split(',')]

            enr = enrichr(pd.DataFrame(sig_genes, columns=["gene_name"]))
            enr["PC"] = pc
            enrichr_enrichments = enrichr_enrichments.append(enr)
        enrichr_enrichments.to_csv(os.path.join(self.results_dir, "PCA.enrichr.csv"), index=False, encoding="utf-8")
        enrichr_enrichments = pd.read_csv(os.path.join(self.results_dir, "PCA.enrichr.csv"))
        lola_enrichments.to_csv(os.path.join(self.results_dir, "PCA.lola.csv"), index=False, encoding="utf-8")

        # Plots

        # p-value distributions
        g = sns.FacetGrid(data=pd.melt(-np.log10(p_values), var_name="PC", value_name="-log10(p-value)"), col="PC", col_wrap=5)
        g.map(sns.distplot, "-log10(p-value)", kde=False)
        g.map(plt.axvline, x=-np.log10(alpha), linestyle="--")
        g.add_legend()
        g.fig.savefig(os.path.join(self.results_dir, "PCA.PC_pvalues.distplot.svg"), bbox_inches="tight")

        # Volcano plots (loading vs p-value)
        # get PCA loadings
        pca = PCA()
        pca.fit(X.T)
        loadings = pd.DataFrame(pca.components_.T, index=X.index, columns=range(1, X.shape[1] + 1))
        loadings.to_csv(os.path.join(self.results_dir, "PCA.loadings.csv"), index=True, encoding="utf-8")

        melted_loadings = pd.melt(loadings.reset_index(), var_name="PC", value_name="loading", id_vars=["index"])
        melted_loadings["PC"] = melted_loadings["PC"].astype(str)
        melted_loadings = melted_loadings.set_index(["index", "PC"])
        melted_p_values = pd.melt((-np.log10(p_values)).reset_index(), var_name="PC", value_name="-log10(p-value)", id_vars=["index"]).set_index(["index", "PC"])
        melted = melted_loadings.join(melted_p_values)

        g = sns.FacetGrid(data=melted.dropna().reset_index(), col="PC", col_wrap=5, sharey=False, sharex=False)
        g.map(plt.scatter, "loading", "-log10(p-value)", s=2, alpha=0.5)
        g.map(plt.axhline, y=-np.log10(alpha), linestyle="--")
        g.add_legend()
        g.fig.savefig(os.path.join(self.results_dir, "PCA.PC_pvalues_vs_loading.scatter.png"), bbox_inches="tight", dpi=300)

        # Plot enrichments
        # LOLA
        # take top n per PC
        import string
        lola_enrichments["set_id"] = lola_enrichments[
            ["collection", "description", "cellType", "tissue", "antibody", "treatment"]].astype(str).apply(string.join, axis=1)

        top = lola_enrichments.set_index('set_id').groupby("PC")['pValueLog'].nlargest(50)
        top_ids = top.index.get_level_values('set_id').unique()

        pivot = pd.pivot_table(
            lola_enrichments,
            index="set_id", columns="PC", values="pValueLog").fillna(0)
        pivot.index = pivot.index.str.replace(" nan", "").str.replace("blueprint blueprint", "blueprint").str.replace("None", "")
        top_ids = top_ids.str.replace(" nan", "").str.replace("blueprint blueprint", "blueprint").str.replace("None", "")

        g = sns.clustermap(
            pivot.ix[top_ids],
            cbar_kws={"label": "Enrichment: -log10(p-value)"}, cmap="Spectral_r",
            col_cluster=True)
        for tick in g.ax_heatmap.get_xticklabels():
            tick.set_rotation(90)
        for tick in g.ax_heatmap.get_yticklabels():
            tick.set_rotation(0)
        g.fig.savefig(os.path.join(self.results_dir, "PCA.PC_pvalues.lola_enrichments.svg"), bbox_inches="tight", dpi=300)

        g = sns.clustermap(
            pivot.ix[top_ids],
            cbar_kws={"label": "Enrichment: p-value z-score"},
            col_cluster=True, z_score=0)
        for tick in g.ax_heatmap.get_xticklabels():
            tick.set_rotation(90)
        for tick in g.ax_heatmap.get_yticklabels():
            tick.set_rotation(0)
        g.fig.savefig(os.path.join(self.results_dir, "PCA.PC_pvalues.lola_enrichments.z_score.svg"), bbox_inches="tight", dpi=300)

        # Enrichr
        for gene_set_library in enrichr_enrichments["gene_set_library"].drop_duplicates():
            enr = enrichr_enrichments[enrichr_enrichments["gene_set_library"] == gene_set_library]

            top = enr.set_index('description').groupby("PC")['p_value'].nsmallest(20)
            top_ids = top.index.get_level_values('description').unique()

            pivot = pd.pivot_table(enr, index="description", columns="PC", values="p_value").fillna(1)
            pivot.index = pivot.index.str.extract("(.*)[,\_\(].*", expand=False).str.replace("_Homo sapiens", "")
            top_ids = top_ids.str.extract("(.*)[,\_\(].*", expand=False).str.replace("_Homo sapiens", "")

            g = sns.clustermap(
                -np.log10(pivot.ix[top_ids]), cmap='BuGn',
                cbar_kws={"label": "Enrichment: -log10(p-value)"}, col_cluster=True, figsize=(6, 15))
            for tick in g.ax_heatmap.get_xticklabels():
                tick.set_rotation(90)
            for tick in g.ax_heatmap.get_yticklabels():
                tick.set_rotation(0)
            g.fig.savefig(os.path.join(self.results_dir, "PCA.PC_pvalues.enrichr_enrichments.{}.svg".format(gene_set_library)), bbox_inches="tight", dpi=300)

            g = sns.clustermap(
                -np.log10(pivot.ix[top_ids]),
                cbar_kws={"label": "Enrichment: p-value z-score"}, col_cluster=True, z_score=0, figsize=(6, 15))
            for tick in g.ax_heatmap.get_xticklabels():
                tick.set_rotation(90)
            for tick in g.ax_heatmap.get_yticklabels():
                tick.set_rotation(0)
            g.fig.savefig(os.path.join(self.results_dir, "PCA.PC_pvalues.enrichr_enrichments.{}.z_score.svg".format(gene_set_library)), bbox_inches="tight", dpi=300)

    def characterize_regions_structure(self, df, prefix, output_dir, universe_df=None):
        # use all sites as universe
        if universe_df is None:
            universe_df = self.coverage_annotated

        # make output dirs
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        # compare genomic regions and chromatin_states
        enrichments = pd.DataFrame()
        for i, var in enumerate(['genomic_region', 'chromatin_state']):
            # prepare:
            # separate comma-delimited fields:
            df_count = Counter(df[var].str.split(',').apply(pd.Series).stack().tolist())
            df_universe_count = Counter(universe_df[var].str.split(',').apply(pd.Series).stack().tolist())

            # divide by total:
            df_count = {k: v / float(len(df)) for k, v in df_count.items()}
            df_universe_count = {k: v / float(len(universe_df)) for k, v in df_universe_count.items()}

            # join data, sort by subset data
            both = pd.DataFrame([df_count, df_universe_count], index=['subset', 'all']).T
            both = both.sort("subset")
            both['region'] = both.index
            data = pd.melt(both, var_name="set", id_vars=['region']).replace(np.nan, 0)

            # sort for same order
            data.sort('region', inplace=True)

            # g = sns.FacetGrid(col="region", data=data, col_wrap=3, sharey=True)
            # g.map(sns.barplot, "set", "value")
            # plt.savefig(os.path.join(output_dir, "%s_regions.%s.svg" % (prefix, var)), bbox_inches="tight")

            fc = pd.DataFrame(np.log2(both['subset'] / both['all']), columns=['value'])
            fc['variable'] = var

            # append
            enrichments = enrichments.append(fc)

        # save
        enrichments.to_csv(os.path.join(output_dir, "%s_regions.region_enrichment.csv" % prefix), index=True)

    def characterize_regions_function(self, df, output_dir, prefix, data_dir="data", universe_file=None):
        from toolkit.general import bed_to_fasta, meme_ame, lola, enrichr, standard_scale

        # use all sites as universe
        if universe_file is None:
            universe_file = self.sites

        # make output dirs
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # save to bed
        bed_file = os.path.join(output_dir, "%s_regions.bed" % prefix)
        df[['chrom', 'start', 'end']].to_csv(bed_file, sep="\t", header=False, index=False)
        # save as tsv
        tsv_file = os.path.join(output_dir, "%s_regions.tsv" % prefix)
        df[['chrom', 'start', 'end']].reset_index().to_csv(tsv_file, sep="\t", header=False, index=False)

        # export ensembl gene names
        df['gene_name'].str.split(",").apply(pd.Series, 1).stack().drop_duplicates().to_csv(os.path.join(output_dir, "%s_genes.symbols.txt" % prefix), index=False)
        # export ensembl gene names
        df['ensembl_gene_id'].str.split(",").apply(pd.Series, 1).stack().drop_duplicates().to_csv(os.path.join(output_dir, "%s_genes.ensembl.txt" % prefix), index=False)

        # export gene symbols with scaled absolute fold change
        if "log2FoldChange" in df.columns:
            df["score"] = standard_scale(abs(df["log2FoldChange"]))
            df["abs_fc"] = abs(df["log2FoldChange"])

            d = df[['gene_name', 'score']].sort_values('score', ascending=False)

            # split gene names from score if a reg.element was assigned to more than one gene
            a = (
                d['gene_name']
                .str.split(",")
                .apply(pd.Series, 1)
                .stack()
            )
            a.index = a.index.droplevel(1)
            a.name = 'gene_name'
            d = d[['score']].join(a)
            # reduce various ranks to mean per gene
            d = d.groupby('gene_name').mean().reset_index()
            d.to_csv(os.path.join(output_dir, "%s_genes.symbols.score.csv" % prefix), index=False)

        # Motifs
        # de novo motif finding - enrichment
        fasta_file = os.path.join(output_dir, "%s_regions.fa" % prefix)
        bed_to_fasta(bed_file, fasta_file)

        meme_ame(fasta_file, output_dir)

        # Lola
        try:
            lola(bed_file, universe_file, output_dir)
        except:
            print("LOLA analysis for %s failed!" % prefix)

        # Enrichr
        results = enrichr(df[['chrom', 'start', 'end', "gene_name"]])

        # Save
        results.to_csv(os.path.join(output_dir, "%s_regions.enrichr.csv" % prefix), index=False, encoding='utf-8')


def metagene_plot(bams, labels, output_prefix, region="genebody", genome="hg19"):
    from pypiper import NGSTk
    import textwrap
    import os
    tk = NGSTk()

    job_name = output_prefix
    job_file = output_prefix + ".sh"
    job_log = output_prefix + ".log"

    # write ngsplot config file to disk
    config_file = os.path.join(os.environ['TMPDIR'], "ngsplot_config.txt")
    with open(config_file, "w") as handle:
        for i in range(len(bams)):
            handle.write("\t".join([bams[i], "-1", labels[i]]) + "\n")

    cmd = tk.slurm_header(job_name, job_log, queue="mediumq", time="1-10:00:00", mem_per_cpu=8000, cpus_per_task=8)

    # build plot command
    if region == "genebody":
        cmd += """xvfb-run ngs.plot.r -G {0} -R {1} -C {2} -O {3} -L 3000 -GO km\n""".format(genome, region, config_file, output_prefix)
    elif region == "tss":
        cmd += """xvfb-run ngs.plot.r -G {0} -R {1} -C {2} -O {3} -L 3000 -FL 300\n""".format(genome, region, config_file, output_prefix)

    cmd += tk.slurm_footer()

    # write job to file
    with open(job_file, 'w') as handle:
        handle.writelines(textwrap.dedent(cmd))

    tk.slurm_submit_job(job_file)


def global_changes(samples, trait="knockout"):
    import glob
    import re

    output_dir = os.path.join("data", "merged")
    # sel_samples = [s for s in samples if not pd.isnull(getattr(s, trait))]
    # groups = sorted(list(set([getattr(s, trait) for s in sel_samples])))
    groups = [os.path.basename(re.sub(".merged.sorted.bam", "", x)) for x in glob.glob(output_dir + "/*.merged.sorted.bam")]

    for region in ["genebody", "tss"]:
        print(metagene_plot(
            [os.path.abspath(os.path.join(output_dir, group + ".merged.sorted.bam")) for group in groups],
            groups,
            os.path.abspath(os.path.join(output_dir, "%s.metaplot" % region)),
            region=region
        ))


def nucleosome_changes(analysis, samples):
    # select only ATAC-seq samples
    df = analysis.prj.sheet.df[analysis.prj.sheet.df["library"] == "ATAC-seq"]

    groups = list()
    for attrs, index in df.groupby(["library", "cell_line", "knockout", "clone"]).groups.items():
        name = "_".join([a for a in attrs if not pd.isnull(a)])
        groups.append(name)
    groups = sorted(groups)

    # nucleosomes per sample
    nucpos_metrics = {
        "z-score": 4,
        "nucleosome_occupancy_estimate": 5,
        "lower_bound_for_nucleosome_occupancy_estimate": 6,
        "upper_bound_for_nucleosome_occupancy_estimat": 7,
        "log_likelihood_ratio": 8,
        "normalized_nucleoatac_signal": 9,
        "cross-correlation_signal_value_before_normalization": 10,
        "number_of_potentially_nucleosome-sized_fragments": 11,
        "number_of_fragments_smaller_than_nucleosome_sized": 12,
        "fuzziness": 13
    }
    # nucleosome-free regions per sample
    nfrpos_metrics = {
        "mean_occupancy": 4,
        "minimum_upper_bound_occupancy": 5,
        "insertion_density": 6,
        "bias_density": 7,
    }
    for data_type, metrics in [("nucpos", nucpos_metrics), ("nfrpos", nfrpos_metrics)]:
        figs = list()
        axizes = list()
        for metric in metrics:
            fig, axis = plt.subplots(5, 6, figsize=(6 * 4, 5 * 4), sharex=True, sharey=True)
            figs.append(fig)
            axizes.append(axis.flatten())

        counts = pd.Series()
        for i, group in enumerate(groups):
            print(data_type, group)
            s = pd.read_csv(
                os.path.join("results", "nucleoatac", group, group + ".{}.bed.gz".format(data_type)),
                sep="\t", header=None)
            counts[group] = s.shape[0]

            for j, (metric, col) in enumerate(metrics.items()):
                print(data_type, group, metric, col)
                sns.distplot(s[col - 1].dropna(), hist=False, ax=axizes[j][i])
                axizes[j][i].set_title(group)

        for i, metric in enumerate(metrics):
            sns.despine(figs[i])
            figs[i].savefig(os.path.join("results", "nucleoatac", "plots", "{}.{}.svg".format(data_type, metric)), bbox_inches="tight")

        fig, axis = plt.subplots(1, 1, figsize=(1 * 4, 1 * 4))
        sns.barplot(counts, counts.index, orient="horizontal", ax=axis, color=sns.color_palette("colorblind")[0])
        axis.set_yticklabels(axis.get_yticklabels(), rotation=0)
        axis.set_xlabel("Calls")
        sns.despine(fig)
        fig.savefig(os.path.join("results", "nucleoatac", "plots", "{}.count_per_sample.svg".format(data_type)), bbox_inches="tight")

    # fragment distribution
    for data_type in ["InsertionProfile", "InsertSizes", "fragmentsizes"]:
        fig, axis = plt.subplots(5, 6, figsize=(6 * 4, 5 * 4))
        axis = axis.flatten()

        data = pd.DataFrame()
        for i, group in enumerate(groups):
            print(data_type, group)
            s = pd.read_csv(
                os.path.join("results", "nucleoatac", group, group + ".{}.txt".format(data_type)),
                sep="\t", header=None, squeeze=True, skiprows=5 if data_type == "fragmentsizes" else 0)
            if data_type == "InsertionProfile":
                a = (len(s.index) - 1) / 2.
                s.index = np.arange(-a, a + 1)
            if data_type == "fragmentsizes":
                s = s.squeeze()
            data[group] = s

            axis[i].plot(s)
            axis[i].set_title(group)
        sns.despine(fig)
        fig.savefig(os.path.join("results", "nucleoatac", "plots", "{}.svg".format(data_type)), bbox_inches="tight")

        norm_data = data.apply(lambda x: x / data['ATAC-seq_HAP1_WT_C631'])
        if data_type == "fragmentsizes":
            norm_data = norm_data.loc[50:, :]
        fig, axis = plt.subplots(5, 6, figsize=(6 * 4, 5 * 4), sharey=True)
        axis = axis.flatten()
        for i, group in enumerate(groups):
            print(data_type, group)
            axis[i].plot(norm_data[group])
            axis[i].set_title(group)
        sns.despine(fig)
        fig.savefig(os.path.join("results", "nucleoatac", "plots", "{}.WT_norm.svg".format(data_type)), bbox_inches="tight")

    # Vplots and
    v_min, v_max = (105, 251)
    # Vplots over WT
    for data_type in ["VMat"]:
        fig, axis = plt.subplots(5, 6, figsize=(6 * 4, 5 * 4))
        fig2, axis2 = plt.subplots(5, 6, figsize=(6 * 4, 5 * 4), sharey=True)
        axis = axis.flatten()
        axis2 = axis2.flatten()

        group = 'ATAC-seq_HAP1_WT_C631'
        wt = pd.read_csv(
            os.path.join("results", "nucleoatac", group, group + ".{}".format(data_type)),
            sep="\t", header=None, skiprows=7)
        wt.index = np.arange(v_min, v_max)
        a = (len(wt.columns) - 1) / 2.
        wt.columns = np.arange(-a, a + 1)
        wt = wt.loc[0:300, :]

        for i, group in enumerate(groups):
            print(data_type, group)
            m = pd.read_csv(
                os.path.join("results", "nucleoatac", group, group + ".{}".format(data_type)),
                sep="\t", header=None, skiprows=7)
            m.index = np.arange(v_min, v_max)
            a = (len(m.columns) - 1) / 2.
            m.columns = np.arange(-a, a + 1)
            m = m.loc[0:300, :]

            n = m / wt

            axis[i].imshow(m.sort_index(ascending=False))
            axis[i].set_title(group)
            axis2[i].imshow(n.sort_index(ascending=False))
            axis2[i].set_title(group)
        sns.despine(fig)
        sns.despine(fig2)
        fig.savefig(os.path.join("results", "nucleoatac", "plots", "{}.svg".format(data_type)), bbox_inches="tight")
        fig2.savefig(os.path.join("results", "nucleoatac", "plots", "{}.WT_norm.svg".format(data_type)), bbox_inches="tight")


def investigate_nucleosome_positions(self, samples, cluster=True):
    df = self.prj.sheet.df[self.prj.sheet.df["library"] == "ATAC-seq"]
    groups = list()
    for attrs, index in df.groupby(["library", "cell_line", "knockout", "clone"]).groups.items():
        name = "_".join([a for a in attrs if not pd.isnull(a)])
        groups.append(name)
    groups = sorted(groups)

    def get_differential(diff, conditions_for, directions_for):
        # filter conditions
        d = diff[diff['comparison'].isin(conditions_for)]

        # filter directions
        d = pd.concat([d[f(d['log2FoldChange'], x)] for f, x in directions_for])

        # make bed format
        df = pd.Series(d.index.str.split(":")).apply(lambda x: pd.Series([x[0]] + x[1].split("-"))).drop_duplicates().reset_index(drop=True)
        df[0] = df[0].astype(str)
        df[1] = df[1].astype(np.int64)
        df[2] = df[2].astype(np.int64)
        return df

    def center_window(bedtool, width=1000):
        chroms = ["chr" + str(x) for x in range(1, 23)] + ["chrX", "chrY"]

        sites = list()
        for site in bedtool:
            if site.chrom in chroms:
                mid = site.start + ((site.end - site.start) / 2)
                sites.append(pybedtools.Interval(site.chrom, mid - (width / 2), (mid + 1) + (width / 2)))
        return pybedtools.BedTool(sites)

    def center_series(series, width=1000):
        mid = series[1] + ((series[2] - series[1]) / 2)
        return pd.Series([series[0], mid - (width / 2), (mid + 1) + (width / 2)])

    # Regions to look at
    regions_pickle = os.path.join(self.results_dir, "nucleoatac", "all_types_of_regions.pickle")
    if os.path.exists():
        regions = pickle.load(open(regions_pickle, "rb"))
    else:
        regions = dict()
        # All accessible sites
        out = os.path.join(self.results_dir, "nucleoatac", "all_sites.bed")
        center_window(self.sites).to_dataframe()[['chrom', 'start', 'end']].to_csv(out, index=False, header=None, sep="\t")
        regions["all_sites"] = out

        # get bed file of promoter proximal sites
        promoter_sites = os.path.join(self.results_dir, "nucleoatac", "promoter_proximal.bed")
        self.coverage_annotated[self.coverage_annotated['distance'].astype(int) < 5000][['chrom', 'start', 'end']].to_csv(
            promoter_sites, index=False, header=None, sep="\t")
        regions["promoter"] = promoter_sites

        # All accessible sites - that are distal
        out = os.path.join(self.results_dir, "nucleoatac", "distal_sites.bed")
        center_window(self.sites.intersect(pybedtools.BedTool(promoter_sites), v=True, wa=True)).to_dataframe()[['chrom', 'start', 'end']].to_csv(out, index=False, header=None, sep="\t")
        regions["distal_sites"] = out

        # Differential sites
        diff = pd.read_csv(os.path.join("results", "deseq_knockout", "deseq_knockout.knockout.csv"), index_col=0)
        diff = diff[(diff["padj"] < 0.01) & (abs(diff["log2FoldChange"]) > 1.)]

        # Loosing accessibility with ARID1A/SMARCA4 KO
        less_ARID1ASMARCA4 = get_differential(
            diff,
            ['ARID1A-WT', 'SMARCA4-WT'],
            [(np.less_equal, -1), (np.less_equal, -1)]).apply(center_series, axis=1)
        out = os.path.join(self.results_dir, "nucleoatac", "diff_sites.less_ARID1ASMARCA4.bed")
        less_ARID1ASMARCA4.to_csv(out, index=False, header=None, sep="\t")
        regions["diff_sites.less_ARID1ASMARCA4"] = out

        # Gaining accessibility with ARID1A/SMARCA4 KO
        more_ARID1ASMARCA4 = get_differential(
            diff,
            ['ARID1A-WT', 'SMARCA4-WT'],
            [(np.greater_equal, 1), (np.greater_equal, 1)]).apply(center_series, axis=1)
        out = os.path.join(self.results_dir, "nucleoatac", "diff_sites.more_ARID1ASMARCA4.bed")
        more_ARID1ASMARCA4.to_csv(out, index=False, header=None, sep="\t")
        regions["diff_sites.more_ARID1ASMARCA4"] = out

        # TFBSs
        tfs = ["CTCF", "BCL", "SMARC", "POU5F1", "SOX2", "NANOG", "TEAD4"]
        for tf in tfs:
            tf_bed = pybedtools.BedTool("/home/arendeiro/resources/genomes/hg19/motifs/TFs/{}.true.bed".format(tf))
            out = os.path.join(self.results_dir, "nucleoatac", "tfbs.%s.bed" % tf)
            center_window(tf_bed.intersect(self.sites, wa=True)).to_dataframe()[['chrom', 'start', 'end']].to_csv(out, index=False, header=None, sep="\t")
            regions["tfbs.%s" % tf] = out

        pickle.dump(regions, open(regions_pickle, "wb"))

    # Launch jobs
    for group in groups:
        output_dir = os.path.join(self.results_dir, "nucleoatac", group)

        # Signals to measure in regions
        signal_files = [
            ("signal", os.path.join(self.data_dir, "merged", group + ".merged.sorted.bam")),
            ("nucleosome", os.path.join(self.data_dir, "merged", group + ".nucleosome_reads.bam")),
            ("nucleosome_free", os.path.join(self.data_dir, "merged", group + ".nucleosome_free_reads.bam")),
            ("nucleoatac", os.path.join("results", "nucleoatac", group, group + ".nucleoatac_signal.smooth.bedgraph.gz")),
            ("dyads", os.path.join("results", "nucleoatac", group, group + ".nucpos.bed.gz"))
        ]
        for region_name, bed_file in regions.items():
            for label, signal_file in signal_files:
                print(group, region_name, label)
                # run job
                run_coverage_job(bed_file, signal_file, label, ".".join([group, region_name, label]), output_dir, window_size=2001)
                # run vplot
                if label == "signal":
                    run_vplot_job(bed_file, signal_file, ".".join([group, region_name, label]), output_dir)

    # Collect signals
    signals = pd.DataFrame(columns=['group', 'region', 'label'])
    # signals = pd.read_csv(os.path.join(self.results_dir, "nucleoatac", "collected_coverage.csv"))

    import re
    for group in [g for g in groups if any([re.match(".*%s.*" % x, g) for x in ["C631", "HAP1_ARID1", "HAP1_SMARCA"]])]:
        output_dir = os.path.join(self.results_dir, "nucleoatac", group)
        signal_files = [
            # ("signal", os.path.join(self.data_dir, "merged", group + ".merged.sorted.bam")),
            # ("nucleosome", os.path.join(self.data_dir, "merged", group + ".nucleosome_reads.bam")),
            # ("nucleosome_free", os.path.join(self.data_dir, "merged", group + ".nucleosome_free_reads.bam")),
            ("nucleoatac", os.path.join("results", "nucleoatac", group, group + ".nucleoatac_signal.smooth.bedgraph.gz")),
            # ("dyads", os.path.join("results", "nucleoatac", group, group + ".nucpos.bed.gz"))
        ]
        for region_name, bed_file in [regions.items()[-1]]:
            for label, signal_file in signal_files:
                # Skip already done
                if len(signals[
                        (signals["group"] == group) &
                        (signals["region"] == region_name) &
                        (signals["label"] == label)
                ]) > 0:
                    print("Continuing", group, region_name, label)
                    continue

                print(group, region_name, label)
                df = pd.read_csv(os.path.join(output_dir, "{}.coverage_matrix.csv".format(".".join([group, region_name, label, label]))), index_col=0)
                df = df.mean(0).reset_index(name="value").rename(columns={"index": "distance"})
                df["group"] = group
                df["region"] = region_name
                df["label"] = label
                signals = signals.append(df, ignore_index=True)
    signals.to_csv(os.path.join(self.results_dir, "nucleoatac", "collected_coverage.csv"), index=False)

    signals = pd.read_csv(os.path.join(self.results_dir, "nucleoatac", "collected_coverage.csv"))

    signals = signals[(signals["distance"] > -400) & (signals["distance"] < 400)]

    # plot together
    region_order = sorted(signals["region"].unique(), reverse=True)
    group_order = sorted(signals["group"].unique(), reverse=True)
    label_order = sorted(signals["label"].unique(), reverse=True)

    # raw
    g = sns.FacetGrid(signals, hue="group", col="region", row="label", hue_order=group_order, row_order=label_order, col_order=region_order, sharex=False, sharey=False)
    g.map(plt.plot, "distance", "value")
    g.add_legend()
    g.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.raw_mean_coverage.svg"), bbox_inches="tight")

    # normalized
    signals["norm_values"] = signals.groupby(["region", "label", "group"])["value"].apply(lambda x: (x - x.mean()) / x.std())

    g = sns.FacetGrid(signals, hue="group", col="region", row="label", hue_order=group_order, row_order=label_order, col_order=region_order, sharex=False, sharey=False)
    g.map(plt.plot, "distance", "norm_values")
    g.add_legend()
    g.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.norm_mean_coverage.svg"), bbox_inches="tight")

    # normalized smoothed
    signals["norm_smooth_values"] = signals.groupby(["region", "label", "group"])["value"].apply(lambda x: pd.rolling_window(((x - x.mean()) / x.std()), 10))

    g = sns.FacetGrid(signals, hue="group", col="region", row="label", hue_order=group_order, row_order=label_order, col_order=region_order, sharex=False, sharey=False)
    g.map(plt.plot, "distance", "norm_smooth_values")
    g.add_legend()
    g.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.norm_mean_coverage.smooth.svg"), bbox_inches="tight")

    #
    # specific regions/samples
    specific_signals = signals[
        (signals["group"].str.contains("ARID1|SMARCA|C631")) &
        (~signals["group"].str.contains("OV90|GFP")) &

        # (signals["region"].str.contains("diff_sites")) &

        (signals["label"] == "nucleoatac")
    ]

    region_order = sorted(specific_signals["region"].unique(), reverse=True)
    group_order = sorted(specific_signals["group"].unique(), reverse=True)
    label_order = sorted(specific_signals["label"].unique(), reverse=True)

    g = sns.FacetGrid(specific_signals, hue="group", col="region", col_wrap=4, hue_order=group_order, row_order=label_order, col_order=region_order, sharex=False, sharey=False)
    g.map(plt.plot, "distance", "value")
    g.add_legend()
    g.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.specific.extended.svg"), bbox_inches="tight")

    # normalized (centered), zoom in center
    specific_signals["norm_values"] = specific_signals.groupby(["region", "label", "group"])["value"].apply(lambda x: (x - x.mean()) / x.std())
    specific_signals = specific_signals[
        (specific_signals["distance"] < 200) &
        (specific_signals["distance"] > -200)]
    g = sns.FacetGrid(specific_signals, hue="group", col="region", row="label", hue_order=group_order, row_order=label_order, col_order=region_order, sharex=False, sharey=False)
    g.map(plt.plot, "distance", "norm_values")
    g.add_legend()
    g.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.specific.norm.svg"), bbox_inches="tight")

    #

    # Violinplots of nucleosome occupancies

    # Heatmap of nucleosome occupancies
    # Collect signals
    sel_groups = [x for x in groups if "OV90" not in x and "GFP" not in x and ("ARID1" in x or "SMARCA" in x or "WT" in x)]
    regions = pickle.load(open(regions_pickle, "rb"))
    sel_regions = {k: v for k, v in regions.items() if "diff" in k}

    # get parameters based on WT accessibility
    region_order = dict()
    region_vmax = dict()
    region_norm_vmax = dict()
    output_dir = os.path.join(self.results_dir, "nucleoatac", "ATAC-seq_HAP1_WT_C631")
    for region_name in sel_regions.keys():
        df = pd.read_csv(os.path.join(output_dir, "{}.coverage_matrix.csv".format(".".join(["ATAC-seq_HAP1_WT_C631", region_name, "nucleoatac", "nucleoatac"]))), index_col=0)
        # vmax
        region_vmax[region_name] = np.percentile(df, 95)
        region_norm_vmax[region_name] = np.percentile((df - df.mean(0)) / df.std(0), 95)
        # region order
        region_order[region_name] = df.sum(axis=1).sort_values().index  # sorted by mean
        # region_order[region_name] = g.dendrogram_row.dendrogram

    # plot all
    fig, axis = plt.subplots(len(sel_regions), len(sel_groups), figsize=(len(sel_groups) * 4, len(sel_regions) * 4))
    fig2, axis2 = plt.subplots(len(sel_regions), len(sel_groups), figsize=(len(sel_groups) * 4, len(sel_regions) * 4))
    for j, group in enumerate(sorted(sel_groups, reverse=True)):
        output_dir = os.path.join(self.results_dir, "nucleoatac", group)
        signal_files = [
            ("nucleoatac", os.path.join("results", "nucleoatac", group, group + ".nucleoatac_signal.smooth.bedgraph.gz"))
        ]
        for i, (region_name, bed_file) in enumerate(sel_regions.items()):
            for label, signal_file in signal_files:
                print(group, region_name, label)
                df = pd.read_csv(os.path.join(output_dir, "{}.coverage_matrix.csv".format(".".join([group, region_name, label, label]))), index_col=0)

                d = df.ix[region_order[region_name]]
                axis[i, j].imshow(
                    d,
                    norm=None, cmap="inferno", vmax=region_vmax[region_name], extent=[-500, 500, 0, 10], aspect="auto")  # aspect=100
                d_norm = (d - d.mean(0)) / d.std(0)
                axis2[i, j].imshow(
                    d_norm,
                    norm=None, cmap="inferno", vmax=region_norm_vmax[region_name], extent=[-500, 500, 0, 10], aspect="auto")  # aspect=100
                for ax in [axis, axis2]:
                    ax[i, j].set_title(group)
                    ax[i, j].set_xlabel("distance")
                    ax[i, j].set_ylabel(region_name)
    sns.despine(fig, top=True, right=True, left=True, bottom=True)
    sns.despine(fig2, top=True, right=True, left=True, bottom=True)
    fig.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.specific.heatmap.png"), bbox_inches="tight", dpi=300)
    fig.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.specific.heatmap.svg"), bbox_inches="tight")
    fig2.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.specific.heatmap.centered.png"), bbox_inches="tight", dpi=300)
    fig2.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "collected_coverage.specific.heatmap.centered.svg"), bbox_inches="tight")


def phasograms(self, samples, max_dist=10000, rolling_window=50, plotting_window=(0, 500)):
    from toolkit.general import detect_peaks

    df = self.prj.sheet.df[self.prj.sheet.df["library"] == "ATAC-seq"]
    groups = list()
    for attrs, index in df.groupby(["library", "cell_line", "knockout", "clone"]).groups.items():
        name = "_".join([a for a in attrs if not pd.isnull(a)])
        groups.append(name)
    groups = sorted(groups)

    def difference_matrix(a):
        x = np.reshape(a, (len(a), 1))
        return x - x.transpose()

    distances = dict()

    for group in groups:
        print(group)
        # Get dyad calls from nucleoatac
        df = pd.read_csv(os.path.join(self.results_dir, "nucleoatac", group, group + ".nucpos.bed.gz"), sep="\t", header=None)

        # separate by chromosome (groupby?)
        # count pairwise distance
        dists = list()
        for chrom in df[0].unique():
            d = abs(difference_matrix(df[df[0] == chrom][1]))
            dd = d[(d < max_dist) & (d != 0)]
            dists += dd.tolist()
        distances[group] = dists

    pickle.dump(distances, open(os.path.join(self.results_dir, "nucleoatac", "phasogram.distances.pickle"), "wb"), protocol=pickle.HIGHEST_PROTOCOL)
    distances = pickle.load(open(os.path.join(self.results_dir, "nucleoatac", "phasogram.distances.pickle"), "rb"))

    # Plot distances between dyads
    from scipy.ndimage.filters import gaussian_filter1d
    n_rows = n_cols = int(np.ceil(np.sqrt(len(groups))))
    n_rows -= 1
    fig, axis = plt.subplots(n_rows, n_cols, sharex=True, sharey=True, figsize=(n_cols * 3, n_rows * 2))
    fig2, axis2 = plt.subplots(n_rows, n_cols, sharex=True, sharey=True, figsize=(n_cols * 3, n_rows * 2))
    axis = axis.flatten()
    axis2 = axis2.flatten()
    for i, group in enumerate(groups):
        # Count frequency of dyad distances
        x = pd.Series(distances[group])
        y = x.value_counts().sort_index()
        y = y.ix[range(plotting_window[0], plotting_window[1])]
        y /= y.sum()

        # Find peaks
        y2 = pd.Series(gaussian_filter1d(y, 5), index=y.index)
        peak_indices = detect_peaks(y2.values, mpd=73.5)[:3]
        print(group, y2.iloc[peak_indices].index)

        # Plot distribution and peaks
        axis[i].plot(y.index, y, color="black", alpha=0.6, linewidth=0.5)
        axis[i].plot(y2.index, y2, color=sns.color_palette("colorblind")[0], linewidth=1)
        axis[i].scatter(y2.iloc[peak_indices].index, y2.iloc[peak_indices], s=25, color="orange")
        for peak in y2.iloc[peak_indices].index:
            axis[i].axvline(peak, color="black", linestyle="--")
        axis[i].set_title(group)

        # Transform into distances between nucleosomes
        # Plot distribution and peaks
        axis2[i].plot(y.index - 147, y, color="black", alpha=0.6, linewidth=0.5)
        axis2[i].plot(y2.index - 147, y2, color=sns.color_palette("colorblind")[0], linewidth=1)
        axis2[i].scatter(y2.iloc[peak_indices].index - 147, y2.iloc[peak_indices], s=25, color="orange")
        for peak in y2.iloc[peak_indices].index:
            axis2[i].axvline(peak - 147, color="black", linestyle="--")
        axis2[i].set_title(group)
    sns.despine(fig)
    sns.despine(fig2)
    fig.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "phasograms.dyad_distances.peaks.svg"), bbox_inches="tight")
    fig2.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "phasograms.nucleosome_distances.peaks.svg"), bbox_inches="tight")

    # Get NFR per knockout
    lengths = dict()

    for group in groups:
        print(group)
        # Get NFR calls from nucleoatac
        df = pd.read_csv(os.path.join(self.results_dir, "nucleoatac", group, group + ".nfrpos.bed.gz"), sep="\t", header=None)
        # Get lengths
        lengths[group] = (df[2] - df[1]).tolist()

    pickle.dump(lengths, open(os.path.join(self.results_dir, "nucleoatac", "nfr.lengths.pickle"), "wb"), protocol=pickle.HIGHEST_PROTOCOL)
    lengths = pickle.load(open(os.path.join(self.results_dir, "nucleoatac", "nfr.lengths.pickle"), "rb"))

    # plot NFR lengths
    from scipy.ndimage.filters import gaussian_filter1d
    n_rows = n_cols = int(np.ceil(np.sqrt(len(groups))))
    n_rows -= 1
    fig, axis = plt.subplots(n_rows, n_cols, sharex=True, sharey=True, figsize=(n_cols * 3, n_rows * 2))
    axis = axis.flatten()
    for i, group in enumerate(groups):
        # Count NFR lengths
        x = pd.Series(lengths[group])
        y = x.value_counts().sort_index()
        y = y.ix[range(plotting_window[0], 300)]
        y /= y.sum()

        # Find peaks
        y2 = pd.Series(gaussian_filter1d(y, 5), index=y.index)
        peak_indices = [detect_peaks(y2.values, mpd=73.5)[0]]
        print(group, y2.iloc[peak_indices].index)

        # Plot distribution and peaks
        axis[i].plot(y.index, y, color="black", alpha=0.6, linewidth=0.5)
        axis[i].plot(y2.index, y2, color=sns.color_palette("colorblind")[0], linewidth=1)
        axis[i].scatter(y2.iloc[peak_indices].index, y2.iloc[peak_indices], s=25, color="orange")
        for peak in y2.iloc[peak_indices].index:
            axis[i].axvline(peak, color="black", linestyle="--")
        axis[i].set_title(group)

    sns.despine(fig)
    fig.savefig(os.path.join(self.results_dir, "nucleoatac", "plots", "phasograms.nfr_lengths.peaks.svg"), bbox_inches="tight")


def run_coverage_job(bed_file, bam_file, coverage_type, name, output_dir, window_size=1001):
    from pypiper import NGSTk
    tk = NGSTk()
    job_file = os.path.join(output_dir, "%s.run_enrichment.sh" % name)
    log_file = os.path.join(output_dir, "%s.run_enrichment.log" % name)

    cmd = """#!/bin/bash
#SBATCH --partition=shortq
#SBATCH --ntasks=1
#SBATCH --time=12:00:00

#SBATCH --cpus-per-task=2
#SBATCH --mem=8000
#SBATCH --nodes=1

#SBATCH --job-name=baf-kubicek-run_enrichment_{}
#SBATCH --output={}

#SBATCH --mail-type=end
#SBATCH --mail-user=

# Start running the job
hostname
date

cd /home/arendeiro/baf-kubicek/

python /home/arendeiro/jobs/run_profiles.py \
--bed-file {} \
--bam-file {} \
--coverage-type {} \
--window-size {} \
--name {} \
--output-dir {}

date
""".format(
        name,
        log_file,
        bed_file,
        bam_file,
        coverage_type,
        window_size,
        name,
        output_dir
    )

    # write job to file
    with open(job_file, 'w') as handle:
        handle.writelines(cmd)

    tk.slurm_submit_job(job_file)


def run_vplot_job(bed_file, bam_file, name, output_dir):
    from pypiper import NGSTk
    tk = NGSTk()
    job_file = os.path.join(output_dir, "%s.run_enrichment.sh" % name)
    log_file = os.path.join(output_dir, "%s.run_enrichment.log" % name)

    cmd = """#!/bin/bash
#SBATCH --partition=mediumq
#SBATCH --ntasks=1
#SBATCH --time=1-12:00:00

#SBATCH --cpus-per-task=8
#SBATCH --mem=24000
#SBATCH --nodes=1

#SBATCH --job-name=baf-kubicek-run_enrichment_{name}
#SBATCH --output={log}

#SBATCH --mail-type=end
#SBATCH --mail-user=

# Start running the job
hostname
date

\t\t# Remove everything to do with your python and env.  Even reset your home dir
\t\tunset PYTHONPATH
\t\tunset PYTHON_HOME
\t\tmodule purge
\t\tmodule load python/2.7.6
\t\tmodule load slurm
\t\tmodule load gcc/4.8.2

\t\tENV_DIR=/scratch/users/arendeiro/nucleoenv
\t\texport HOME=$ENV_DIR/home

\t\t# Activate your virtual env
\t\texport VIRTUALENVWRAPPER_PYTHON=/cm/shared/apps/python/2.7.6/bin/python
\t\tsource $ENV_DIR/bin/activate

\t\t# Prepare to install new python packages
\t\texport PATH=$ENV_DIR/install/bin:$PATH
\t\texport PYTHONPATH=$ENV_DIR/install/lib/python2.7/site-packages


cd /home/arendeiro/baf-kubicek/

pyatac vplot \
--bed {peaks} \
--bam {bam} \
--out {output_prefix}.vplot \
--cores 8 \
--lower 30 \
--upper 1000 \
--flank 500 \
--scale \
--plot_extra

pyatac sizes \
--bam {bam} \
--bed {peaks} \
--out {output_prefix}.sizes  \
--lower 30 \
--upper 1000

pyatac bias \
--fasta ~/resources/genomes/hg19/hg19.fa \
--bed {peaks} \
--out {output_prefix}.bias \
--cores 8

pyatac bias_vplot \
--bed {peaks} \
--bg {output_prefix}.bias.Scores.bedgraph.gz \
--fasta ~/resources/genomes/hg19/hg19.fa \
--sizes {output_prefix}.sizes.fragmentsizes.txt \
--out {output_prefix}.bias_vplot \
--cores 8 \
--lower 30 \
--upper 1000 \
--flank 500 \
--scale \
--plot_extra

date
""".format(
        name=name,
        log=log_file,
        peaks=bed_file,
        bam=bam_file,
        output_prefix=os.path.join(output_dir, name))

    # write job to file
    with open(job_file, 'w') as handle:
        handle.writelines(cmd)

    tk.slurm_submit_job(job_file)


def piq_prepare_motifs(
        motifs_file="data/external/jaspar_human_motifs.txt",
        output_dir="footprinting",
        piq_source_dir="/home/arendeiro/workspace/piq-single/",
        motif_numbers=None):
    """
    Prepare motifs for footprinting (done once per genome).
    """
    import textwrap
    from pypiper import NGSTk
    tk = NGSTk()

    motifs_dir = os.path.join(output_dir, "motifs")
    jobs_dir = os.path.join(motifs_dir, "jobs")

    for path in [output_dir, motifs_dir, jobs_dir]:
        if not os.path.exists(path):
            os.makedirs(path)

    if motif_numbers is None:
        n_motifs = open(motifs_file, "r").read().count(">")
        motif_numbers = range(1, n_motifs + 1)

    for motif in motif_numbers:
        # skip if exists
        a = os.path.exists(os.path.join(motifs_dir, "{}.pwmout.RData".format(motif)))
        b = os.path.exists(os.path.join(motifs_dir, "{}.pwmout.rc.RData".format(motif)))
        if a and b:
            continue

        # prepare job
        log_file = os.path.join(jobs_dir, "piq_preparemotifs.motif{}.slurm.log".format(motif))
        job_file = os.path.join(jobs_dir, "piq_preparemotifs.motif{}.slurm.sh".format(motif))
        cmd = tk.slurm_header(
            "piq_preparemotifs.{}".format(motif), log_file,
            cpus_per_task=1, queue="shortq")

        # change to PIQ dir (required in PIQ because of hard-coded links in code)
        cmd += """cd {}\n\t\t""".format(piq_source_dir)

        # Actual PIQ command
        cmd += """Rscript {}/pwmmatch.exact.r""".format(piq_source_dir)
        cmd += " {}/common.r".format(piq_source_dir)
        cmd += " {}".format(os.path.abspath(motifs_file))
        cmd += " " + str(motif)
        cmd += """ {}\n""".format(motifs_dir)

        # write job to file
        with open(job_file, 'w') as handle:
            handle.writelines(textwrap.dedent(cmd))

        tk.slurm_submit_job(job_file)


def piq_prepare_bams(
        bam_files,
        group_name,
        output_dir="footprinting",
        piq_source_dir="/home/arendeiro/workspace/piq-single/"):
    """
    Prepare group of bam files (can be one too) for footprinting.
    """
    import textwrap
    from pypiper import NGSTk
    tk = NGSTk()

    cache_dir = os.path.join(output_dir, "piq_cache")
    jobs_dir = os.path.join(cache_dir, "jobs")

    for path in [output_dir, cache_dir, jobs_dir]:
        if not os.path.exists(path):
            os.makedirs(path)

    merged_bam = os.path.join(cache_dir, "{}.merged.bam".format(group_name))
    merged_cache = os.path.join(cache_dir, "{}.merged.RData".format(group_name))
    log_file = os.path.join(jobs_dir, "piq_preparebams.{}.slurm.log".format(group_name))
    job_file = os.path.join(jobs_dir, "piq_preparebams.{}.slurm.sh".format(group_name))

    # Build job
    cmd = tk.slurm_header(
        "piq_preparebams.{}".format(group_name), log_file,
        cpus_per_task=12, queue="longq", time="7-12:00:00", mem_per_cpu=4000
    )

    # merge all bam files
    cmd += """sambamba merge -t 12 {0} {1}\n""".format(merged_bam, " ".join(bam_files))

    # change to PIQ dir (required in PIQ because of hard-coded links in code)
    cmd += """\t\tcd {}\n""".format(piq_source_dir)

    # Actual PIQ command
    cmd += """\t\tRscript {0}/bam2rdata.r {0}/common.r {1} {2}\n""".format(
        piq_source_dir, merged_cache, " ".join(bam_files))

    # slurm footer
    cmd += tk.slurm_footer()

    # write job to file
    with open(job_file, 'w') as handle:
        handle.writelines(textwrap.dedent(cmd))

    tk.slurm_submit_job(job_file)


def footprint(
        group_name, motif_numbers,
        output_dir="footprinting",
        piq_source_dir="/home/arendeiro/workspace/piq-single/"):
    import textwrap
    from pypiper import NGSTk
    tk = NGSTk()

    motifs_dir = os.path.join(output_dir, "motifs")
    cache_dir = os.path.join(output_dir, "piq_cache")
    foots_dir = os.path.join(output_dir, "footprint_calls")
    tmp_dir = os.path.join(foots_dir, "tmp_output")
    jobs_dir = os.path.join(foots_dir, "jobs")
    merged_cache = os.path.join(cache_dir, "{}.merged.RData".format(group_name))

    for path in [output_dir, motifs_dir, cache_dir, foots_dir, tmp_dir, jobs_dir]:
        if not os.path.exists(path):
            os.makedirs(path)

    for motif_number in motif_numbers:
        if not os.path.exists(os.path.join(motifs_dir, "{}.pwmout.RData".format(motif_number))):
            print("PIQ file for motif {} does not exist".format(motif_number))
            continue

        label = "{}.{}".format(group_name, motif_number)

        t_dir = os.path.join(tmp_dir, label)
        if not os.path.exists(t_dir):
            os.mkdir(t_dir)
        log_file = os.path.join(jobs_dir, "piq_footprinting.{}.motif{}.slurm.log".format(group_name, motif_number))
        job_file = os.path.join(jobs_dir, "piq_footprinting.{}.motif{}.slurm.sh".format(group_name, motif_number))

        # prepare slurm job header
        cmd = tk.slurm_header(
            "piq_footprinting.{}.motif{}".format(group_name, motif_number),
            log_file, cpus_per_task=1, queue="shortq", mem_per_cpu=8000)

        # change to PIQ dir (required in PIQ because of hard-coded links in code)
        cmd += """cd {}\n""".format(piq_source_dir)

        # Actual PIQ command
        # footprint
        cmd += """\t\tRscript {0}/pertf.r {0}/common.r {1} {2} {3} {4} {5}\n""".format(
            piq_source_dir, motifs_dir, t_dir, foots_dir, merged_cache, motif_number)

        # slurm footer
        cmd += tk.slurm_footer()

        # write job to file
        with open(job_file, 'w') as handle:
            handle.writelines(textwrap.dedent(cmd))

        tk.slurm_submit_job(job_file)
