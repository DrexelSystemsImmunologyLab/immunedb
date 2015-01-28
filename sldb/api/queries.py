import datetime
import json
import math
import re
import numpy as np
from sqlalchemy.sql.expression import false, true
from sqlalchemy import desc, distinct, inspect
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import false
from sqlalchemy.ext.declarative import DeclarativeMeta

import sldb.util.lookups as lookups
from sldb.common.models import *
from sldb.identification.v_genes import VGene
from sldb.common.mutations import Mutations, MutationType


_clone_filters = {
    'clones_all': lambda q: q,
    'clones_functional': lambda q: q.filter(
        Clone.cdr3_num_nts % 3 == 0),
    'clones_nonfunctional': lambda q: q.filter(
        Clone.cdr3_num_nts % 3 != 0),
}


def _fields_to_dict(fields, row):
    d = {}
    for f in fields:
        d[f] = getattr(row, f)
    return d


def _subject_to_dict(subject):
    return {
        'study': {
            'id': subject.study.id,
            'name': subject.study.name
        },
        'identifier': subject.identifier,
        'id': subject.id
    }


def _sample_to_dict(sample):
    d = _fields_to_dict(['id', 'name', 'info', 'subset', 'tissue',
                         'disease', 'lab', 'experimenter'], sample)
    d['date'] = sample.date.strftime('%Y-%m-%d')
    d['subject'] = _subject_to_dict(sample.subject)
    return d


def _clone_to_dict(clone):
    d = _fields_to_dict(['id', 'cdr3_nt'], clone)
    d['group'] = {
        'id': clone.group.id,
        'v_gene': clone.group.v_gene,
        'j_gene': clone.group.j_gene,
        'cdr3_aa': clone.group.cdr3_aa,
        'cdr3_num_nts': clone.group.cdr3_num_nts,
        'subject': _subject_to_dict(clone.group.subject),
    }
    d['germline'] = clone.group.germline[0:VGene.CDR3_OFFSET] + \
        clone.cdr3_nt + clone.group.germline[VGene.CDR3_OFFSET +
                                             clone.group.cdr3_num_nts:]
    return d


def get_all_studies(session):
    result = {}
    for sample in session.query(Sample).order_by(Sample.date):
        if session.query(Sequence).filter(
                Sequence.sample == sample).first() is not None:
            status = 'reads'
        elif session.query(NoResult).filter(
                NoResult.sample == sample).first() is not None:
            status = 'noreads'
        else:
            status = 'unprocessed'

        if status in ('reads', 'noreads'):
            if sample.study.id not in result:
                result[sample.study.id] = {
                    'id': sample.study.id,
                    'name': sample.study.name,
                    'info': sample.study.info,
                    'samples': []
                }
            sample_dict = _sample_to_dict(sample)
            stats = session.query(SampleStats.sequence_cnt,
                                  SampleStats.in_frame_cnt,
                                  SampleStats.stop_cnt,
                                  SampleStats.functional_cnt,
                                  SampleStats.no_result_cnt).filter(
                SampleStats.sample_id == sample.id,
                SampleStats.outliers == true(),
                SampleStats.full_reads == false(),
                SampleStats.filter_type == 'all').first()
            if stats is not None:
                sample_dict['status'] = status
                sample_dict['sequence_cnt'] = stats.sequence_cnt
                sample_dict['in_frame_cnt'] = stats.in_frame_cnt
                sample_dict['stop_cnt'] = stats.stop_cnt
                sample_dict['functional_cnt'] = stats.functional_cnt
                sample_dict['no_result_cnt'] = stats.no_result_cnt
            else:
                sample_dict['status'] = 'processing'
            result[sample.study.id]['samples'].append(sample_dict)

    return result


def get_all_clones(session, filters, order_field, order_dir, paging=None):
    """Gets a list of all clones"""
    res = []
    clone_q = session.query(Clone)

    if filters is not None:
        for key, value in filters.iteritems():
            if value is None:
                continue
            value = str(value).strip()
            if len(value) > 0 and value is not None:
                if key == 'min_cdr3_num_nts':
                    clone_q = clone_q.filter(Clone.cdr3_num_nts >= int(value))
                elif key == 'max_cdr3_num_nts':
                    clone_q = clone_q.filter(Clone.cdr3_num_nts <= int(value))
                elif key == 'id':
                    clone_q = clone_q.filter(Clone.id == int(value))
                elif key == 'group_id':
                    clone_q = clone_q.filter(Clone.group_id == int(value))
                else:
                    if hasattr(Clone, key):
                        c = Clone
                    else:
                        c = Clone.group
                    clone_q = clone_q.filter(
                        getattr(c, key).like(value.replace('*', '%')))

    if paging is not None:
        page, per_page = paging
        clone_q = clone_q.offset((page - 1) * per_page).limit(per_page)

    for c in clone_q:
        total_unique = 0
        total_all = 0
        stats_comb = []
        for stat in session.query(CloneStats).filter(
                CloneStats.clone_id == c.id):
            stats_comb.append({
                'sample': {
                    'id': stat.sample.id,
                    'name': stat.sample.name
                },
                'unique_sequences': int(stat.unique_cnt),
                'total_sequences': int(stat.total_cnt)
            })
            total_unique += int(stat.unique_cnt)
            total_all += int(stat.total_cnt)
        clone_dict = _clone_to_dict(c)
        clone_dict['unique_sequences'] = total_unique
        clone_dict['total_sequences'] = total_all
        clone_dict['stats'] = stats_comb
        res.append(clone_dict)

    return res


def compare_clones(session, uids):
    """Compares sequences within clones by determining their mutations"""
    clones = {}
    clone_muts = {}
    for clone_id, sample_ids in uids.iteritems():
        clone = session.query(Clone).filter(Clone.id == clone_id).first()
        germline = clone.group.germline
        if clone_id not in clones:
            clone_muts[clone_id] = Mutations(germline, clone.cdr3_nt)
            clones[clone_id] = {
                'clone': _clone_to_dict(clone),
                'mutation_stats': {},
                'seqs': []
            }
        mutations = clone_muts[clone_id]

        start_ptrn = re.compile('[N\-]*')

        q = session.query(
            Sequence,
            func.sum(Sequence.copy_number).label('copy_number'))\
            .filter(Sequence.clone_id == clone_id)
        if None not in sample_ids:
            q = q.filter(Sequence.sample_id.in_(sample_ids))
        q = q.order_by(desc('copy_number')).group_by(
            Sequence.sequence_replaced)

        for seqr in q:
            seq = seqr.Sequence
            read_start = start_ptrn.match(seq.sequence)
            if read_start is None:
                read_start = 0
            else:
                read_start = read_start.span()[1]

            muts = mutations.add_sequence(seq.sequence_replaced)
            clones[clone_id]['seqs'].append({
                'seq_id': seq.seq_id,
                'sample': {
                    'id': seq.sample.id,
                    'name': seq.sample.name,
                },
                'junction_nt': seq.junction_nt,
                'sequence': seq.sequence_replaced,
                'read_start': read_start,
                'copy_number': int(seqr.copy_number),
                'mutations': muts,
                'v_extent': seq.v_length + seq.num_gaps + seq.pad_length,
                'j_length': seq.j_length,
            })

        region_stats, pos_stats = mutations.get_aggregate()
        clones[clone_id]['mutation_stats']['regions'] = region_stats
        clones[clone_id]['mutation_stats']['positions'] = pos_stats

    return clones


def get_clone_tree(session, clone_id):
    return session.query(Clone.tree).filter(Clone.id == clone_id).first()


def get_clone_overlap(session, filter_type, ctype, limit,
                      paging=None):
    """Gets a list of clones and the samples in `samples` which they appear"""
    fltr = _clone_filters[filter_type]
    res = []
    q = fltr(session.query(
        CloneStats,
        func.count(CloneStats.unique_cnt).label('unique'),
        func.sum(CloneStats.total_cnt).label('total'),
    ).join(Clone))

    if ctype == 'samples':
        q = q.filter(CloneStats.sample_id.in_(limit))
    elif ctype == 'subject':
        q = q.filter(CloneStats.sample.has(subject_id=limit))

    q = q.group_by(CloneStats.clone_id).order_by(desc('total'))

    if paging is not None:
        page, per_page = paging
        q = q.offset((page - 1) * per_page).limit(per_page)

    for clone in q:
        selected_samples = []
        other_samples = []
        for stat in session.query(CloneStats).filter(
                CloneStats.clone_id == clone.CloneStats.clone_id).order_by(
                desc(CloneStats.total_cnt)):
            data = {
                'id': stat.sample_id,
                'name': stat.sample.name,
                'unique_sequences': stat.unique_cnt,
                'total_sequences': stat.total_cnt
            }
            if ctype == 'subject' or stat.sample_id in limit:
                selected_samples.append(data)
            else:
                other_samples.append(data)

        res.append({
            'unique_sequences': int(clone.unique),
            'total_sequences': int(clone.total),
            'clone': _clone_to_dict(clone.CloneStats.clone),
            'selected_samples': selected_samples,
            'other_samples': other_samples,
        })

    if paging:
        return res
    return res


def get_clones_in_samples(session, samples):
    return map(lambda e: e.id,
               session.query(
                   distinct(Sequence.clone_id).label('id')).filter(
                   Sequence.sample_id.in_(samples)))


def get_clones_in_subject(session, subject_id):
    return map(lambda e: e.id, session.query(Clone).filter(
        Clone.subject_id == subject_id))


def get_v_usage(session, samples, filter_type, outliers, full_reads):
    """Gets the V-Gene usage percentages for samples"""
    data = {}
    headers = []
    for s in session.query(SampleStats)\
            .filter(SampleStats.filter_type == filter_type,
                    SampleStats.outliers == outliers,
                    SampleStats.full_reads == full_reads,
                    SampleStats.sample_id.in_(samples)):
        dist = json.loads(s.v_gene_dist)
        data[s.sample.name] = {}
        total = 0
        for v in dist:
            total += v[1]

        for v in dist:
            name, occ = v
            name = '|'.join(
                sorted(set(map(lambda s: s.split('*')[0], name.split('|')))))
            if name not in headers:
                headers.append(name)

            data[s.sample.name][name] = round(100 * occ / float(total), 2)

    return data, headers


def get_all_subjects(session, paging):
    q = session.query(Subject)

    if paging is not None:
        page, per_page = paging
        q = q.offset((page - 1) * per_page).limit(per_page)

    subjects = []
    for subject in q:
        seqs = session.query(func.sum(SampleStats.sequence_cnt))\
            .filter(
                SampleStats.sample.has(subject=subject),
                SampleStats.filter_type == 'all',
                SampleStats.outliers == true(),
                SampleStats.full_reads == false()).scalar()
        if seqs is None:
            continue

        subjects.append({
            'id': subject.id,
            'identifier': subject.identifier,
            'study': {
                'id': subject.study.id,
                'name': subject.study.name
            },
            'total_samples': session.query(func.count(Sample.id)).filter(
                Sample.subject == subject).scalar(),
            'unique_seqs': int(seqs),
            'total_clones': session.query(func.count(Clone.id)).filter(
                Clone.subject_id == subject.id).scalar()
        })

    return subjects


def get_subject(session, sid):
    s = session.query(Subject).filter(Subject.id == sid).first()
    samples = []

    for sample in session.query(Sample).filter(Sample.subject_id == sid):
        stats = session.query(SampleStats).filter(
            SampleStats.filter_type == 'all',
            SampleStats.sample_id == sample.id,
            SampleStats.outliers == true(),
            SampleStats.full_reads == false()).first()
        sample_dict = {
            'id': sample.id,
            'name': sample.name,
            'date': sample.date.strftime('%Y-%m-%d'),
        }
        if stats is not None:
            sample_dict['valid_cnt'] = stats.sequence_cnt
            sample_dict['no_result_cnt'] = stats.no_result_cnt
            sample_dict['functional_cnt'] = stats.functional_cnt
        samples.append(sample_dict)

    subject = {
        'id': s.id,
        'identifier': s.identifier,
        'study': {
            'id': s.study.id,
            'name': s.study.name,
        },
        'samples': samples,
    }

    return subject


def get_stats(session, samples, include_outliers, full_reads):
    counts = {}
    stats = {}
    dist_fields = [
        'v_match_dist', 'v_length_dist', 'j_match_dist',
        'j_length_dist', 'v_gene_dist', 'j_gene_dist',
        'cdr3_length_dist', 'copy_number_dist']
    cnt_fields = ['sequence_cnt', 'in_frame_cnt', 'stop_cnt', 'functional_cnt',
                  'no_result_cnt']
    for stat in session.query(SampleStats).filter(
            SampleStats.sample_id.in_(samples),
            SampleStats.outliers == include_outliers,
            SampleStats.full_reads == full_reads):
        if stat.sample_id not in stats:
            stats[stat.sample_id] = {
                'sample': _sample_to_dict(stat.sample),
                'filters': {},
            }
        if stat.filter_type not in counts:
            counts[stat.filter_type] = 0

        flds = _fields_to_dict(dist_fields + cnt_fields, stat)
        stats[stat.sample_id]['filters'][stat.filter_type] = flds
        counts[stat.filter_type] += stat.sequence_cnt

    return {'counts': counts, 'stats': stats}


def get_sequence(session, sample_id, seq_id):
    seq = session.query(Sequence)\
        .filter(Sequence.sample_id == sample_id,
                Sequence.seq_id == seq_id).first()
    if seq is None:
        seq = session.query(DuplicateSequence)\
            .filter(DuplicateSequence.seq_id == seq_id).first()

    ret = _fields_to_dict([
        'seq_id', 'alignment', 'v_gene', 'j_gene',
        'junction_nt', 'junction_aa', 'germline', 'v_match', 'j_match',
        'v_length', 'j_length', 'in_frame', 'functional', 'stop',
        'copy_number', 'sequence', 'pre_cdr3_length', 'pre_cdr3_match',
        'post_cdr3_length', 'post_cdr3_match', 'pad_length', 'num_gaps',
        'probable_indel_or_misalign'], seq)
    ret['sample'] = _sample_to_dict(seq.sample)

    ret['v_extent'] = ret['v_length'] + ret['num_gaps'] + ret['pad_length']

    if seq.clone is None:
        ret['clone'] = None
    else:
        ret['clone'] = _clone_to_dict(seq.clone)

    muts = Mutations(seq.germline, seq.junction_nt)
    ret['mutations'] = muts.add_sequence(seq.sequence)

    ret['duplicates'] = []
    ret['total_copy_number'] = ret['copy_number']
    for dup in session.query(Sequence).filter(
            Sequence.sequence == seq.sequence,
            Sequence.sample.has(subject_id=seq.sample.subject_id),
            Sequence.seq_id != seq_id)\
            .order_by(Sequence.sample_id):
        ret['duplicates'].append({
            'seq_id': dup.seq_id,
            'sample': _sample_to_dict(dup.sample),
            'alignment': dup.alignment,
            'copy_number': dup.copy_number,
        })
        ret['total_copy_number'] += dup.copy_number

    return ret


def get_all_sequences(session, filters, order_field, order_dir, paging=None):
    """Gets a list of all clones"""
    def get_field(key):
        tbls = [Sequence, Subject, Clone]
        for t in tbls:
            if hasattr(t, key):
                return getattr(t, key)

    res = []
    query = session.query(Sequence).join(Sample).outerjoin(Clone)

    if filters is not None:
        for key, value in filters.iteritems():
            if value in [None, True, False]:
                continue
            value = str(value).strip()
            if len(value) > 0 and value is not None:
                if key == 'sample_id':
                    query = query.filter(Sequence.sample_id == int(value))
                elif key == 'in_frame':
                    query = query.filter(Sequence.in_frame == int(value))
                elif key == 'min_copy_number':
                    query = query.filter(Sequence.copy_number >= int(value))
                elif key == 'max_copy_number':
                    query = query.filter(Sequence.copy_number <= int(value))
                else:
                    query = query.filter(get_field(key).like(
                        value.replace('*', '%')))

    if filters is None or 'show_r1' not in filters or not filters['show_r1']:
        query = query.filter(Sequence.alignment == 'R1+R2')
    if (filters is None or 'show_indel' not in filters
            or not filters['show_indel']):
        query = query.filter(Sequence.probable_indel_or_misalign == 0)

    if paging is not None:
        page, per_page = paging
        query = query.offset((page - 1) * per_page).limit(per_page)

    for row in query:
        fields = _fields_to_dict(
            ['seq_id', 'alignment', 'v_gene', 'j_gene', 'v_match', 'j_match',
             'v_length', 'j_length', 'junction_num_nts', 'junction_aa',
             'in_frame', 'functional', 'stop', 'probable_indel_or_misalign'],
            row)

        fields['copy_number'] = int(session.query(
            func.sum(Sequence.copy_number)).filter(
                Sequence.unique_id == row.unique_id).scalar())

        fields['sample'] = _sample_to_dict(row.sample)
        res.append(fields)

    return res
