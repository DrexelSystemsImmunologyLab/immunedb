import csv
import re

from Bio import SeqIO

from sldb.common.models import Sample, Sequence, Study, Subject
from sldb.identification.v_genes import VGene, get_common_seq
import sldb.util.funcs as funcs
import sldb.util.lookups as lookups

IMPORT_HEADERS = {
    'study_name': 'The name of the study [Required]',

    'sample_name': 'The name of the sample [Required]',
    'subject': 'The name of the subject [Required]',

    'subset': 'The cell subset of the sample',
    'tissue': 'The tissue from which the sample was gathered',
    'disease': 'The disease present in the subject',
    'lab': 'The lab which gathered the sample',
    'experimenter': 'The individual who gathered the sample',

    'seq_id': 'A unique sequence identifier [Required]',
    'alignment': 'Read type for the sequence (R1, R2, or R1+R2) [Required]',
    'indel': 'A boolean indicating if the sequence has an indel',

    'v_gene': 'V-gene name [Required]',
    'v_length': 'Length of V gene EXCLUDING leading padding [Required]',
    'v_match': 'Number of nucleotides matching V-gene germline [Required]',

    'j_gene': 'J-gene name [Required]',
    'j_length': 'Length of J gene [Required]',
    'j_match': 'Number of nucleotides matching the J-gene germline [Required]',

    'pre_cdr3_length': 'Length of the V-gene before the CDR3.  If not '
        'specified, assumed to be equal to `v_length`',
    'pre_cdr3_match': 'Number of nucleotides matching the V-gene germline '
        'before the CDR3.  If not specified, assumed to be equal to `v_match`',

    'post_cdr3_length': 'Length of the J-gene after the CDR3.  If not '
        'specified, assumed to be equal to `j_length`',
    'post_cdr3_match': 'Number of nucleotides matching the J-gene germline '
        'after the CDR3.  If not specified, assumed to be equal to `j_match`',

    'in_frame': 'A boolean indicating if the sequence is in-frame [Required]',
    'functional': 'A boolean indicating if the sequence is functional '
                  '[Required]',
    'stop': 'A boolean indicating if the sequences contains any stop codons '
            '[Required]',
    'copy_number': 'The number of times the sequence occurred in the sample '
                   '[Required]',

    'sequence': 'The full, IMGT aligned sequence [Required]',
    'cdr3_nts': 'The CDR3 nucleotides [Required]',
    'cdr3_aas': 'The CDR3 amino-acids.  If not specified, the `cdr3_nts` will '
        'be converted to an amino-acid string with unknowns replaced with Xs',
}


class ImportException(Exception):
    pass


def _is_true(v):
    return v.upper() in ('T', 'TRUE')


class DelimitedImporter(object):
    def __init__(self, session, mappings, defaults, v_germlines, j_germlines,
                 j_offset, fail_action):
        self._session = session
        self._mappings = mappings
        self._defaults = defaults
        self._v_germlines = v_germlines
        self._j_germlines = j_germlines
        self._j_offset = j_offset
        self._fail_action = fail_action

        self._cached_studies = {}
        self._cached_subjects = {}

    def _get_header_name(self, field_name):
        if field_name in self._mappings:
            return self._mappings[field_name]
        return field_name

    def _get_value(self, field_name, row, throw=True):
        header = self._get_header_name(field_name)
        if header not in row:
            if header not in self._defaults:
                if throw:
                    raise ImportException(
                        'Header {} for field {} not found.'.format(header,
                            field_name))
                return None
            return self._defaults[header]
        return row[header]

    def _get_models(self, row):
        study_name = self._get_value('study_name', row)
        sample_name = self._get_value('sample_name', row)

        sample_cache_key = (study_name, sample_name)
        if sample_cache_key in self._cached_studies:
            study, sample = self._cached_studies[sample_cache_key]
        else:
            study, new = funcs.get_or_create(self._session, Study,
                                             name=study_name)
            if new:
                print 'Created new study {}'.format(study_name)
                self._session.flush()

            sample, new = funcs.get_or_create(self._session, Sample,
                                              study_id=study.id,
                                              name=sample_name)
            if new:
                print 'Created new sample {}'.format(sample_name)
                sample.date = self._get_value('date', row)
                for field in ('subset', 'tissue', 'disease', 'lab',
                        'experimenter'):
                    setattr(sample, field,
                            self._get_value(field, row, throw=False))

                subject_name = self._get_value('subject', row)
                subject_cache_key = (study_name, subject_name)
                if subject_cache_key in self._cached_subjects:
                    subject = self._cached_subjects[subject_cache_key]
                else:
                    subject, new = funcs.get_or_create(
                        self._session,
                        Subject,
                        study_id=study.id,
                        identifier=subject_name)
                    if new:
                        print 'Created new subject {}'.format(subject_name)
                        self._cached_subjects[subject_cache_key] = subject

                sample.subject = subject
                self._session.flush()

            self._cached_studies[sample_cache_key] = (study, sample)
            # TODO: If not new, verify the rest of the fields are the same
        return study, sample

    def _process_sequence(self, row, study, sample):
        seq = self._get_value('sequence', row).upper().replace('.', '-')
        # Check for duplicate sequence
        existing = self._session.query(Sequence).filter(
            Sequence.sequence == seq,
            Sequence.sample_id == sample.id).first()
        if existing is not None:
            existing.copy_number += int(self._get_value('copy_number', row))
            self._session.flush()
            return

        v_region = seq[:VGene.CDR3_OFFSET]
        pad_length = re.match('[-N]*', v_region).end() or 0
        num_gaps = v_region[pad_length:].count('-')
        cdr3_aas = (self._get_value('cdr3_aas', row, throw=False)
            or lookups.aas_from_nts(self._get_value('cdr3_nts', row)))

        v_germline = get_common_seq(
            [self._v_germlines[v] for v in self._get_value(
                'v_gene', row).split('|')])
        j_germline = get_common_seq(
            [self._j_germlines[v][-self._j_offset:] for v in self._get_value(
                'j_gene', row).split('|')])

        sequence_replaced = ''.join(
            [g if s in ('N', '-') else s for s, g in zip(seq, v_germline)]
        )
        germline = ''.join([
            v_germline[:VGene.CDR3_OFFSET],
            '-' * len(self._get_value('cdr3_nts', row)),
            j_germline
        ])

        seq = Sequence(
            sample=sample,

            seq_id=self._get_value('seq_id', row),
            alignment=self._get_value('alignment', row),
            probable_indel_or_misalign=_is_true(self._get_value('indel', row)),
            v_gene=self._get_value('v_gene', row),
            j_gene=self._get_value('j_gene', row),

            num_gaps=num_gaps,
            pad_length=pad_length,

            v_match=self._get_value('v_match', row),
            v_length=self._get_value('v_length', row),

            j_match=self._get_value('j_match', row),
            j_length=self._get_value('j_length', row),

            pre_cdr3_length=self._get_value(
                'pre_cdr3_length', row, throw=False
            ) or self._get_value('v_length', row),
            pre_cdr3_match=self._get_value(
                'pre_cdr3_match', row, throw=False
            ) or self._get_value('v_match', row),
            post_cdr3_length=self._get_value(
                'post_cdr3_length', row, throw=False
            ) or self._get_value('j_length', row),
            post_cdr3_match=self._get_value(
                'post_cdr3_match', row, throw=False
            ) or self._get_value('j_match', row),

            in_frame=_is_true(self._get_value('in_frame', row)),
            functional=_is_true(self._get_value('functional', row)),
            stop=_is_true(self._get_value('stop', row)),
            copy_number=int(self._get_value('copy_number', row)),

            junction_num_nts=len(self._get_value('cdr3_nts', row)),
            junction_nt=self._get_value('cdr3_nts', row),
            junction_aa=cdr3_aas,
            gap_method='IMGT',

            sequence=seq,
            sequence_replaced=sequence_replaced,
        )

    def process_file(self, fh, delimiter):
        for i, row in enumerate(csv.DictReader(fh, delimiter=delimiter)):
            try:
                study, sample = self._get_models(row)
                self._process_sequence(row, study, sample)
            except Exception as ex:
                if self._fail_action != 'pass':
                    print ('[WARNING] Unable to process row #{}: '
                           'type={}, msg={}').format(
                                i, str(ex.__class__.__name__), ex.message)
                    if self._fail_action == 'fail':
                        raise ex

        self._session.commit()


def _parse_map(lst):
    if lst is None:
        return {}
    mapping = {}
    for arg in lst:
        field, name = arg.split('=', 1)
        mapping[field] = name
    return mapping


def _get_germlines(path):
    germs = {}
    with open(path) as fh:
        for record in SeqIO.parse(fh, 'fasta'):
            germs[record.id] = str(record.seq)
    return germs


def run_delimited_import(session, args):
    mappings = _parse_map(args.mappings)
    defaults = _parse_map(args.defaults)

    v_germlines = _get_germlines(args.v_germlines)
    j_germlines = _get_germlines(args.j_germlines)

    importer = DelimitedImporter(session, mappings, defaults, v_germlines,
                                 j_germlines, args.j_offset, args.fail_action)

    for fn in args.files:
        with open(fn) as fh:
            importer.process_file(fh, args.delimiter)