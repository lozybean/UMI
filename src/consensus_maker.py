#!/usr/bin/env python
# -*- coding: utf-8 -*- \#
"""
@author = 'liangzb'
@date = '2017/11/28 0028'

make consensus bam files from sorted bam file
"""
import json
from collections import defaultdict, Counter
from functools import lru_cache
from multiprocessing import cpu_count
from multiprocessing.pool import ThreadPool

import pysam
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from fastinterval import Interval
from path import Path


class MySegments(object):
    """
    copy attributions of pysam.libcalignedsegment.AlignedSegment objects,
    make sure it can be pickle-able.
    """

    def __init__(self, segment):
        attributions = ['query_name', 'query_length',
                        'query_alignment_sequence', 'query_alignment_qualities',
                        'reference_name', 'reference_start', 'reference_end',
                        'is_read1', 'is_read2',
                        'cigarstring']
        for attr in attributions:
            setattr(self, attr, getattr(segment, attr))


class ConsensusMaker(list):
    """
    a list container for pysam.libcalignedsegment.AlignedSegment objects
    to maker a consensus PE read
    """

    def __init__(self, umi, distance=500,
                 min_qual=10, min_freq=0.6, min_reads=1,
                 qual_n=2, max_n_count=3, max_n_ratio=0.03, **kwargs):
        """
        :param umi: UMI seq
        :param distance: maximum distance can be cached as ONE molecular
        :param min_qual: minimum quality of a base can be used for making consensus
        :param min_freq: minimum frequency to make a consensus base
        :param min_reads: minimum number of reads to make a consensus
        :param qual_n: if can not make a consensus base, N will return, and the qual_n will set as the quality
        :param max_n_count: maximum N count can be exist in the consensus read
        :param max_n_ratio: maximum N ratio can be exist in the consensus read
        """
        super(ConsensusMaker, self).__init__()
        self.umi = umi
        self.distance = distance
        self.min_qual = min_qual
        self.min_freq = min_freq
        self.qual_n = qual_n
        self.min_count = min_reads
        self.max_n_count = max_n_count
        self.max_n_ratio = max_n_ratio

    @property
    def position(self):
        if not self:
            return None
        return self[0].reference_start

    @property
    def chrom(self):
        if not self:
            return None
        return self[0].reference_name

    @property
    def read1_segments(self):
        return [seg for seg in self if seg.is_read1]

    @property
    def read2_segments(self):
        return [seg for seg in self if seg.is_read2]

    def within_distance(self, segment):
        """
        :param segment: current pysam.libcalignedsegment.AlignedSegment object
        :return: True or False
        """
        if not self:
            return True
        if segment.reference_name != self.chrom:
            return False
        if abs(segment.reference_start - self.position) < self.distance:
            return True
        return False

    @staticmethod
    def get_most_common_cigar(segments):
        cigar_count = Counter(seg.cigarstring for seg in segments)
        return cigar_count.most_common(1)[0][0]

    @staticmethod
    def pick_segments_with_cigar(segments, cigarstring):
        return [seg for seg in segments if seg.cigarstring == cigarstring]

    def get_consensus_base(self, bases, quals):
        """
        refer: https://github.com/aryeelab/umi/blob/dev/consolidate.py
        :param bases: list of base: str
        :param quals: list of qual: int
        :return: (consensus fail, consensus base, qual)
        """
        base_num = defaultdict(int)
        base_qual = defaultdict(int)
        for base, qual in zip(bases, quals):
            if qual > self.min_qual:
                base_num[base] += 1
            if qual > base_qual[base]:
                base_qual[base] = qual
        if not base_num:
            return True, 'N', self.qual_n
        most_common_base = max(base_num, key=lambda k: base_num[k])
        freq = base_num[most_common_base] / len(bases)
        if freq > self.min_freq:
            return False, most_common_base, base_qual[most_common_base]
        else:
            return True, 'N', self.qual_n

    def make_consensus(self, segments):
        """
        refer: https://github.com/aryeelab/umi/blob/dev/consolidate.py
        consensus record: Bio.SeqRecord.SeqRecord object
        :return: (making_success, consensus_record, num_segments_for_make_consensus)
        """
        most_common_cigar = self.get_most_common_cigar(segments)
        segments = self.pick_segments_with_cigar(segments, most_common_cigar)
        bases_t = zip(*[seg.query_alignment_sequence for seg in segments])
        quals_t = zip(*[seg.query_alignment_qualities for seg in segments])
        consensus_fail, consensus_seq, consensus_qual = zip(*[self.get_consensus_base(bases, quals)
                                                              for bases, quals in zip(bases_t, quals_t)])
        consensus_fail_count = sum(consensus_fail)
        if consensus_fail_count > self.max_n_count:
            return False, None, 0
        if consensus_fail_count / len(consensus_seq) > self.max_n_ratio:
            return False, None, 0

        consensus_seq = Seq(''.join(consensus_seq))
        consensus_id = f'{self.umi}_cons_{len(segments)}'
        return True, SeqRecord(seq=consensus_seq, letter_annotations={'phred_quality': consensus_qual},
                               id=consensus_id, description=''), len(segments)

    def get_consensus_read(self):
        """
        :return: (read1_consensus or None, read2_consensus or None, STATUS)
        """
        if not self.read1_segments:
            return None, None, 'reads1 unmatched'
        if not self.read2_segments:
            return None, None, 'reads2 unmatched'
        read1_success, read1_consensus, segments_count_for_read1 = self.make_consensus(self.read1_segments)
        read2_success, read2_consensus, segments_count_for_read2 = self.make_consensus(self.read2_segments)
        if not read1_success:
            return None, None, 'reads1 failed'
        if not read2_success:
            return None, None, 'reads2 failed'
        if segments_count_for_read1 < self.min_count:
            return None, None, 'reads1 too less'
        if segments_count_for_read2 < self.min_count:
            return None, None, 'reads2 too less'
        read2_consensus = read2_consensus.reverse_complement()
        read2_consensus.description = ''

        pos_reg = f'{self.chrom}_{self.position}'
        consensus_id = f'{self.umi}:{pos_reg}:{segments_count_for_read1}:{segments_count_for_read2}'
        read1_consensus.id = consensus_id
        read2_consensus.id = consensus_id
        return read1_consensus, read2_consensus, 'success'

    def __repr__(self):
        return f'<CachedSegments {self.umi}:{len(self.read1_segments)}:{len(self.read2_segments)}>'


class ConsensusWorker(object):
    def __init__(self, bam_in, bed_file=None, flank_size=20, threads=cpu_count(), **kwargs):
        self.threads = threads
        bam_file = pysam.AlignmentFile(bam_in, 'rb')
        self.bam_reader = bam_file.fetch(until_eof=True)
        self.bed_file = Path(bed_file)
        self.flank_size = flank_size
        self.kwargs = kwargs
        self.cached_segments = {}
        self.stats = defaultdict(int)

    @property
    @lru_cache(1)
    def intervals(self):
        if self.bed_file is None:
            return {}
        if not self.bed_file.exists:
            raise ValueError(f"can not find the bed file: {self.bed_file}")
        intervals = defaultdict(list)
        with open(self.bed_file) as fp:
            for line in fp:
                chrom, start, end, *_ = line.strip().split('\t')
                intervals[chrom].append(Interval(int(start), int(end), chrom=chrom))
        return {chrom: Interval.merge(intervals[chrom]) for chrom in intervals}

    def within_interval(self, segment):
        if not self.intervals:
            return True
        if segment.reference_name not in self.intervals:
            return False
        current_position = Interval(segment.reference_start or 0,
                                    segment.reference_end or segment.reference_start + segment.query_length,
                                    chrom=segment.reference_name)
        for interval in self.intervals[segment.reference_name]:
            if interval.distance(current_position) < self.flank_size:
                return True
        return False

    @staticmethod
    def get_umi(segment):
        return segment.query_name.split(':')[-1]

    def get_segments(self):
        for segment in self.bam_reader:
            if segment.is_unmapped:
                continue
            segment = MySegments(segment)
            if not self.within_interval(segment):
                self.stats['off target'] += 1
                continue
            umi = self.get_umi(segment)
            if umi not in self.cached_segments:
                self.cached_segments[umi] = ConsensusMaker(umi, **self.kwargs)
            elif self.cached_segments[umi].within_distance(segment):
                self.cached_segments[umi].append(segment)
            else:
                yield self.cached_segments.pop(umi)

    def get_async_results(self, results):
        for i in list(results):
            r = results[i]
            if r.ready():
                r = results.pop(i)
                read1, read2, status = r.get()
                self.stats[status] += 1
                if not read1 or not read2:
                    continue
                yield read1, read2

    def async_get_consensus_read(self):
        pool = ThreadPool(self.threads)

        async_results = {}
        for ind, consensus_maker in enumerate(self.get_segments()):
            async_results[ind] = pool.apply_async(consensus_maker.get_consensus_read)
            for read1, read2 in self.get_async_results(async_results):
                yield read1, read2
        while async_results:
            for read1, read2 in self.get_async_results(async_results):
                yield read1, read2
        pool.close()
        pool.join()

    def async_output_pe_reads(self, read1_file, read2_file):
        with open(read1_file, 'w') as fp1, open(read2_file, 'w') as fp2:
            for read1, read2 in self.async_get_consensus_read():
                fp1.write(read1.format('fastq'))
                fp2.write(read2.format('fastq'))

    def get_consensus_read(self):
        for consensus_maker in self.get_segments():
            read1, read2, status = consensus_maker.get_consensus_read()
            self.stats[status] += 1
            if not read1 or not read2:
                continue
            yield read1, read2

    def output_pe_reads(self, read1_file, read2_file):
        with open(read1_file, 'w') as fp1, open(read2_file, 'w') as fp2:
            for read1, read2 in self.get_consensus_read():
                fp1.write(read1.format('fastq'))
                fp2.write(read2.format('fastq'))

    def output_stats(self, stat_file):
        with open(stat_file, 'w') as fp:
            json.dump(self.stats, fp)
