import argparse
import sys
import csv
import datetime

import sldb.common.config as config
from sldb.common.models import *
from sldb.util.funcs import get_or_create


_mt_remap_headers = {
    'seqID': 'seq_id',
    'in-frame': 'in_frame',
}


_mt_nomap = ['junction_gap_length', 'juncton_gap_length', 'subject',
             'germline', 'cloneID', 'collapsedCloneID', 'withSinglecloneID',
             'mutation_invariate', 'subset', 'tissue', 'disease', 'date',
             'lab', 'experimenter']


def cast_to_type(table_type, field_name, value):
    ftype = getattr(Sequence.__table__.c, field_name).type
    if isinstance(ftype, Boolean):
        return value.upper() == 'T'
    elif isinstance(ftype, Integer):
        return int(value)
    elif isinstance(ftype, Date):
        month, day, year = map(int, value.split('/'))
        return datetime.datetime(year, month, day)
    return str(value)


def _create_sequence_record(row):
    ''' Creates a new sequences record that has proper information '''
    record = Sequence()
    for key, value in row.iteritems():
        if key not in _mt_nomap and value not in ('unknown', 'NaN', ''):
            setattr(record, key, cast_to_type(Sequence, key, value))
    record.v_call = record.v_call.replace('/', '|')
    record.j_call = record.j_call.replace('/', '|')

    return record


def _remap_headers(row):
    if None in row:
        del row[None]

    for remap_from, remap_to in _mt_remap_headers.iteritems():
        if remap_from in row:
            row[remap_to] = row.pop(remap_from)


def _populate_sample(session, study, sample, row):
    sample.subject, _ = get_or_create(
        session, Subject, study_id=study.id,
        identifier=row['subject'])
    sample.date = datetime.datetime.strptime(
        row['date'], '%m/%d/%Y').strftime('%Y-%m-%d')
    sample.subset = row['subset']
    sample.tissue = row['tissue']
    sample.disease = row['disease']
    sample.lab = row['lab']
    sample.experimenter = row['experimenter']


def _fill_with_germ(seq, germline):
    if len(germline) != len(seq.sequence):
        print 'Germline and sequence different lengths'
        print 'SeqID', seq.seq_id
        print 'Germline: ', germline
        print 'Sequence: ', seq.sequence
        return seq

    filled_seq = ''
    for i, c in enumerate(seq.sequence):
        if c.upper() == 'N':
            filled_seq += germline[i].upper()
        else:
            filled_seq += c
    return filled_seq


def _add_mt(session, path, study_name, sample_name, interval):
    ''' Processes the entire master table '''
    study, new = get_or_create(session, Study, name=study_name)
    if new:
        print 'Created new study "{}"'.format(study.name)
    else:
        print 'Study "{}" already exists in MASTER'.format(study.name)
    session.commit()

    sample, new = get_or_create(session, Sample,
                                name=sample_name,
                                study=study)
    if new:
        print '\tCreated new sample "{}" in MASTER'.format(sample.name)
        session.commit()
    else:
        # TODO: Verify the data for the existing sample matches this MT
        exists = session.query(Sequence).filter(
            Sequence.sample == sample).first()
        if exists is not None:
            print ('\tSample "{}" for study already exists in DATA.  '
                   'Skipping.').format(sample.name)
            return None

    with open(path, 'rU') as fh:
        order_to_seq = {}
        for row in csv.DictReader(fh, delimiter='\t'):
            _remap_headers(row)
            order_to_seq[int(row['order'])] = row['seq_id']
        total = len(order_to_seq)
        fh.seek(0)

        for i, row in enumerate(csv.DictReader(fh, delimiter='\t')):
            _remap_headers(row)
            if 'noresult' in row.values():
                session.add(NoResult(seq_id=row['seq_id'], sample=sample))
                sample.no_result_cnt += 1
            else:
                record = _create_sequence_record(row)

                # Check if the sample needs to be populated
                if sample.subject is None:
                    _populate_sample(session, study, sample, row)

                sample.valid_cnt += record.copy_number_iden
                if record.functional:
                    sample.functional_cnt += record.copy_number_iden
                if record.copy_number_iden > 0:
                    record.sample = sample
                    record.sequence_replaced = _fill_with_germ(record,
                                                               row['germline'])

                    session.add(record)
                else:
                    record = DuplicateSequence(
                        sample=sample,
                        identity_seq_id=order_to_seq[record.collapse_to_iden],
                        seq_id=record.seq_id)
                    session.add(record)

            if i > 0 and i % interval == 0:
                session.commit()
                print '\t\tCommitted {} / {} ({}%)'.format(
                    i,
                    total,
                    round(100 * i / float(total), 2))

    session.commit()

    print 'Completed master table'
    return sample


def run_mt2db(session, args):
    for l in sys.stdin:
        study_name, sample_name, date, _ = map(lambda s: s.strip(),
                                               l.split('|'))
        path = '{}/{}/{}/{}'.format(
            args.mt_dir,  study_name, date, sample_name)
        print study_name, sample_name, path
        sample = _add_mt(
            session=session,
            path=path + '/master_table',
            study_name=study_name,
            sample_name=sample_name,
            interval=args.c)
    session.close()
