import argparse
import json
import math
import subprocess
import time

from sqlalchemy import create_engine, desc, distinct
from sqlalchemy.orm import sessionmaker, scoped_session

import bottle
from bottle import route, response, request, install, run

from sldb.api.export import CloneExport, SequenceExport, MutationExporter
import sldb.api.queries as queries
from sldb.common.models import *
from sldb.common.mutations import threshold_mutations
import sldb.util.lookups as lookups
from sldb.util.nested_writer import NestedCSVWriter


class EnableCors(object):
    """A class to enable Cross-Origin Resource Sharing to facilitate AJAX
    requests.

    """
    name = 'enable_cors'
    api = 2

    def apply(self, fn, context):
        def _enable_cors(*args, **kwargs):
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = ('GET, POST')
            response.headers['Access-Control-Allow-Headers'] = (
                'Origin, '
                'Accept, Content-Type, '
                'X-Requested-With, X-CSRF-Token')

            if bottle.request.method != 'OPTIONS':
                return fn(*args, **kwargs)

        return _enable_cors


def _get_arg(key, is_json=True):
    if key not in request.query or len(request.query[key].strip()) == 0:
        return None
    req = request.query[key].strip()
    return json.loads(req) if is_json else req


def _get_paging():
    """Handles paging based on a request's query string"""
    page = _get_arg('page', False) or 1
    per_page = _get_arg('per_page', False) or 10
    page = int(page)
    per_page = int(per_page)
    return page, per_page


def _split(ids, delim=','):
    """Helper function to split a string into an integer array"""
    return map(int, ids.split(delim))


@route('/api/sequences/')
def sequences():
    """Gets a list of all sequences.

    :returns: A list of all sequences
    :rtype: str

    """
    session = scoped_session(session_factory)()
    sequences = queries.get_all_sequences(
        session,
        _get_arg('filter'),
        _get_arg('order_field', False) or 'seq_id',
        _get_arg('order_dir', False) or 'desc',
        _get_paging())
    session.close()
    return json.dumps({'sequences': sequences})


@route('/api/sequence/<sample_id:int>/<seq_id>')
def sequence(sample_id, seq_id):
    """Gets the sequence identified by ``seq_id`` in sample with id
    ``sample_id``.

    :param int sample_id: The sample ID of the sequence
    :param str seq_id: The sequence ID of the sequence

    :returns: The requested sequence if it exists
    :rtype: str

    """
    session = scoped_session(session_factory)()
    seq = queries.get_sequence(session, int(sample_id), seq_id)
    session.close()
    return json.dumps({'sequence': seq})


@route('/api/studies')
def studies():
    """Gets a list of all studies and their associated samples.

    :returns: A list of all studies and their associated samples
    :rtype: str

    """
    session = scoped_session(session_factory)()
    studies = queries.get_all_studies(session)
    session.close()
    return json.dumps({'studies': studies})


@route('/api/subjects')
def subjects():
    """Gets a list of all subjects.

    :returns: A list of all subjects
    :rtype: str

    """
    session = scoped_session(session_factory)()
    subjects = queries.get_all_subjects(session, _get_paging())
    session.close()
    return json.dumps({'subjects': subjects})


@route('/api/subject/<sid:int>')
def subject(sid):
    """Gets the subject with id ``sid``.

    :param int sid: The subject ID to query

    :returns: The requested subject if it exists
    :rtype: str

    """
    session = scoped_session(session_factory)()
    subject = queries.get_subject(session, int(sid))
    session.close()
    return json.dumps({'subject': subject})


@route('/api/clones/')
def clones():
    """Gets a list of all clones.

    :returns: A list of all clones
    :rtype: str

    """
    session = scoped_session(session_factory)()
    clones = queries.get_all_clones(
        session,
        _get_arg('filter'),
        _get_arg('order_field', False) or 'id',
        _get_arg('order_dir', False) or 'desc',
        _get_paging())
    session.close()
    return json.dumps({'clones': clones})


@route('/api/clone/<clone_id:int>')
@route('/api/clone/<clone_id:int>/<sample_ids>')
def clone(clone_id, sample_ids=None):
    """Gets a clone clones, outputting its mutations and other pertinent
    information.

    :param int clone_id: The clone ID
    :param str sample_ids: A comma-separated list of samples to restrict
    analysis

    :returns: Clone information
    :rtype: str

    """
    session = scoped_session(session_factory)()
    clone = queries.get_clone(session, clone_id, sample_ids)
    pressure = queries.get_selection_pressure(session, clone_id, sample_ids)
    session.close()

    return json.dumps({
        'clone': clone,
        'selection_pressure': pressure
    })


@route('/api/clone_tree/<cid:int>')
def clone_tree(cid):
    """ Gets the lineage tree represented by JSON for a clone.

    :param int cid: The clone ID of which to get the lineage tree

    :returns: The lineage tree as JSON
    :rtype: str

    """
    session = scoped_session(session_factory)()
    tree = queries.get_clone_tree(session, cid)
    session.close()
    return tree


@route('/api/clone_overlap/<filter_type>/<samples>')
@route('/api/subject_clones/<filter_type>/<subject:int>')
def clone_overlap(filter_type, samples=None, subject=None):
    """Gets the clones that overlap between a set of samples. If ``samples`` is
    supplied, the overlap of clones between those is returned.  If ``samples``
    is not supplied, the clonal overlap for all samples from ``subject`` is
    returned.

    :param str filter_type: The filter for the clones to apply.  This can \
    currently be ``clones_all`` for all clones, ``clones_functional`` for \
    only    functional clones, or ``clones_nonfunctional`` for only \
    non-functional clones.
    :param str samples: A comma-separated sequences of sample IDs if \
    comparing specific samples.  Otherwise, ``None``.
    :param int subject: The subject ID to use for clonal overlap.  If \
    specified, all samples from the subject are compared.

    :returns: The overlap of clones in the specified sample or subjects
    :rtype: str

    """
    session = scoped_session(session_factory)()
    if samples is not None:
        sids = _split(samples)
    ctype = 'samples' if samples is not None else 'subject'

    exporting = _get_arg('export', False) == 'true'
    paging = _get_paging() if not exporting else None
    clones = queries.get_clone_overlap(
        session, filter_type, ctype,
        sids if samples is not None else subject, paging)
    session.close()

    if exporting:
        csv_mapping = {
            'clone_id': (lambda r: r['clone']['id']),
            'clone_v': (lambda r: r['clone']['group']['v_gene']),
            'clone_j': (lambda r: r['clone']['group']['j_gene']),
            'clone_cdr3_aa': (lambda r: r['clone']['group']['cdr3_aa']),
            'clone_cdr3_num_nts': (
                lambda r: r['clone']['group']['cdr3_num_nts']
            ),
            'clone_subject': (
                lambda r: r['clone']['group']['subject']['identifier']
            ),
        }
        writer = NestedCSVWriter([
            'total_sequences', 'unique_sequences', 'clone_id', 'clone_v',
            'clone_j', 'clone_cdr3_aa', 'clone_cdr3_num_nts', 'clone_subject'
        ], mapping=csv_mapping, streaming=True)

        name = 'overlap_{}.csv'.format(
            time.strftime('%Y-%m-%d-%H-%M'))
        response.headers['Content-Disposition'] = (
            'attachment;filename={}').format(name)

        for clone in clones:
            yield writer.add_row(clone)
    else:
        yield json.dumps({'clones': clones})


@route('/api/stats/<samples>/<include_outliers>/<include_partials>/<grouping>')
def stats(samples, include_outliers, include_partials, grouping):
    """Gets the statistics for a given set of samples both including and
    excluding outliers.

    :param str samples: A comma-separated list of sample IDs for which to \
    gather statistics

    :returns: Statistics for all samples in ``samples``
    :rtype: str

    """
    session = scoped_session(session_factory)()
    samples = _split(samples)
    ret = queries.get_stats(session, samples, include_outliers == 'true',
                            include_partials == 'true', grouping)
    session.close()
    return json.dumps(ret)


@route('/api/modification_log')
def modification_log():
    session = scoped_session(session_factory)()
    q = session.query(ModificationLog).order_by(desc(ModificationLog.datetime))

    paging = _get_paging()
    if paging is not None:
        page, per_page = paging
        q = q.offset((page - 1) * per_page).limit(per_page)

    logs = []
    for log in q:
        logs.append({
            'datetime': log.datetime.strftime('%Y-%m-%d %H:%M:%S'),
            'action_type': log.action_type,
            'info': json.loads(log.info),
        })
    session.close()
    return json.dumps({'logs': logs})


@route('/api/v_usage/<samples>/<filter_type>/<include_outliers>/'
       '<include_partials>/<grouping>/<by_family>')
def v_usage(samples, filter_type, include_outliers, include_partials,
            grouping, by_family):
    """Gets the V usage for samples in a heatmap-formatted array.

    :param str filter_type: The filter type of sequences for the v_usage
    :param str samples: A comma-separated string of sample IDs for v_usage

    :returns: The V usage as an [(x,y)...] JSON array along with x and y
    categories
    :rtype: str

    """
    session = scoped_session(session_factory)()
    data, x_categories, totals = queries.get_v_usage(
        session, _split(samples), filter_type, include_outliers == 'true',
        include_partials == 'true', grouping, by_family == 'true')
    session.close()

    x_categories.sort()
    y_categories = sorted(data.keys())
    array = []
    for i, x in enumerate(x_categories):
        for j, y in enumerate(y_categories):
            usage_for_y = data[y]
            if x in usage_for_y:
                array.append([i, j, usage_for_y[x]])
            else:
                array.append([i, j, 0])

    return json.dumps({
        'x_categories': x_categories,
        'y_categories': y_categories,
        'totals': totals,
        'data': array,
    })


def get_max_point(threshold, data_list):
    """Return the point which is the first point at threshold of the height of
    the curve or above
    """

    target_y = threshold * data_list[-1][1]
    for data in datalist:
        if data[1] >= target_y:
            return data


def format_diversity_csv_output(x, y, s):
    """
    Convert rarefaction output to a format for plotting
    """

    return [[float(row.split(',')[x]), float(row.split(',')[y])]
            for i, row in enumerate(s.split('\n'))
            if row != '' and i != 0]


@route('/api/rarefaction/<sample_ids>/<mode>/<fast_bool>/<start>'
       '/<num_points:int>', methods=['GET'])
def rarefaction(sample_ids, mode, fast_bool, start, num_points):
    """Return the rarefaction curve in json format from a list of sample ids"""

    assert mode in ('sample', 'individual', 'individual_emp')
    session = scoped_session(session_factory)()

    fast_bool = fast_bool == 'true'

    sample_id_list = map(int, sample_ids.split(','))

    if mode == 'sample':
        cids = session.query(
            CloneStats.clone_id, CloneStats.sample_id
        ).filter(CloneStats.sample_id.in_(sample_id_list))
    else:
        cids = session.query(
            CloneStats.clone_id,
            func.sum(CloneStats.unique_cnt).label('cnt')
        ).filter(
            CloneStats.sample_id.in_(sample_id_list)
        ).group_by(CloneStats.clone_id)

    cid_string = ''
    total_num = 0

    samples = set([])
    for cid in cids:
        if mode == 'sample':
            cid_string += '>{}\n{}\n'.format(cid.sample_id, cid.clone_id)
            samples.add(cid.sample_id)
        else:
            cid_string += '\n'.join(
                [str(cid.clone_id) for _ in range(0, cid.cnt)]) + '\n'
            total_num += cid.cnt

    if mode == 'sample':
        interval = 1
    else:
        interval = min(
            max(total_num // int(num_points), 1),
            total_num)

    command = [rf_bin,
               '-a',
               '-d',
               '-c',
               'hi',
               '-t',
               '-I',
               '{} {}'.format(start, interval)]

    if mode == 'sample':
        command.extend(['-s', '-S', '1'])
    else:
        command.extend(['-L'])

    if fast_bool:
        command.extend(['-f'])

    if mode == 'individual_emp':
        command.extend(['-R', _get_arg('reps', False)])

    proc = subprocess.Popen(command,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)

    output = proc.communicate(cid_string)

    result_list = format_diversity_csv_output(4, 5, output[0])
    threshold_point = get_max_point(0.95, result_list)

    session.close()

    return json.dumps({'rarefaction': result_list,
                       'threshold': threshold_point})


@route('/api/diversity/<sample_ids>/<order>/<window>', methods=['GET'])
def diversity(sample_ids, order, window):
    """Return the diversity values in json format from a list of sample ids"""

    session = scoped_session(session_factory)()

    sample_id_list = map(int, sample_ids.split(','))

    # Get sequences here
    seqs = session.query(distinct(Sequence.sequence).label('sequence')).filter(
        Sequence.sample_id.in_(sample_id_list))

    command = [rf_bin,
               '-r', str(order),
               '-w', str(window),
               '-o', 'hi',
               '-t',
               '-L']

    proc = subprocess.Popen(command,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)

    for i, seq in enumerate(seqs):
        proc.stdin.write('{}\n'.format(
            lookups.aas_from_nts(
                seq.sequence, replace_unknowns='-'
            )[:103]))
    session.close()

    output = proc.communicate()

    result_list = format_diversity_csv_output(3, 5, output[0])

    return json.dumps({'diversity': result_list})


@route('/api/data/v_usage/<samples>/<filter_type>/<include_outliers>/'
       '<include_partials>/<grouping>/<by_family>')
@route('/api/data/v_usage/<samples>/<filter_type>/<include_outliers>/'
       '<include_partials>/<grouping>/<by_family>/')
def export_v_usage(samples, filter_type, include_outliers, include_partials,
                   grouping, by_family):
    """Gets the V usage for samples in tab format.

    :param str filter_type: The filter type of sequences for the v_usage
    :param str samples: A comma-separated string of sample IDs for v_usage

    :returns: The V usage as a CSV
    :rtype: str

    """
    session = scoped_session(session_factory)()
    data, x_categories, totals = queries.get_v_usage(
        session, _split(samples), filter_type, include_outliers == 'true',
        include_partials == 'true', grouping, by_family == 'true')
    session.close()

    name = 'v_usage_{}.csv'.format(
        time.strftime('%Y-%m-%d-%H-%M'))
    response.headers['Content-Disposition'] = 'attachment;filename={}'.format(
        name)

    x_categories.sort()
    y_categories = sorted(data.keys())
    array = []
    yield ' ,{}\n'.format(','.join(map(
        lambda v: 'IGHV{}'.format(v), x_categories)))
    for y in y_categories:
        yield '{}'.format(y)
        for x in x_categories:
            if x in data[y]:
                yield ',{}'.format(data[y][x])
            else:
                yield ',0'
        yield '\n'


@route('/api/data/export_clones/<rtype>/<rids>', methods=['GET'])
@route('/api/data/export_clones/<rtype>/<rids>/', methods=['GET'])
def export_clones(rtype, rids):
    """Downloads a tab-delimited file of clones.

    :param str rtype: The type of record to filter the query on.  Currently
        either "sample" or "clone"
    :param str rids: A comma-separated list of IDs of ``rtype`` to export

    :returns: The properly formatted export data
    :rtype: str

    """
    assert rtype in ('sample', 'clone')

    session = scoped_session(session_factory)()
    fields = _get_arg('fields', False).split(',')
    include_total_row = _get_arg('include_total_row', False) == 'true' or False

    name = '{}_{}.csv'.format(
        rtype,
        time.strftime('%Y-%m-%d-%H-%M'))

    response.headers['Content-Disposition'] = 'attachment;filename={}'.format(
        name)

    export = CloneExport(session, rtype, _split(rids), fields,
                         include_total_row)
    for line in export.get_data():
        yield line

    session.close()


@route('/api/data/export_sequences/<eformat>/<rtype>/<rids>', methods=['GET'])
@route('/api/data/export_sequences/<eformat>/<rtype>/<rids>/', methods=['GET'])
def export_sequences(eformat, rtype, rids):
    """Downloads an exported format of specified sequences.

    :param str eformat: The export format to use.  Currently "csv", "orig", and
        "clip" for comma-delimited, FASTA, FASTA with filled in germlines, and
        FASTA in CLIP format respectively
    :param str rtype: The type of record to filter the query on.  Currently
        either "sample" or "clone"
    :param str rids: A comma-separated list of IDs of ``rtype`` to export

    :returns: The properly formatted export data
    :rtype: str

    """

    assert eformat in ('csv', 'fill', 'orig', 'clip')
    assert rtype in ('sample', 'clone')

    session = scoped_session(session_factory)()

    fields = _get_arg('fields', False).split(',')
    min_copy = _get_arg('min_copy_number', False)
    min_copy = int(min_copy) if min_copy is not None else 1

    if eformat == 'csv':
        name = '{}_{}.csv'.format(
            rtype,
            time.strftime('%Y-%m-%d-%H-%M'))
    else:
        if 'seq_id' in fields:
            fields.remove('seq_id')
        name = '{}_{}_{}.fasta'.format(
            rtype,
            eformat,
            time.strftime('%Y-%m-%d-%H-%M'))

    response.headers['Content-Disposition'] = 'attachment;filename={}'.format(
        name)

    export = SequenceExport(
        session, eformat, rtype, _split(rids), fields,
        min_copy=min_copy,
        duplicates=_get_arg('duplicates', False) == 'true',
        noresults=_get_arg('noresults', False) == 'true')
    for line in export.get_data():
        yield line

    session.close()


@route('/api/data/export_mutations/<rtype>/<rids>/<thresh_type>/'
       '<thresh_value:int>', methods=['GET'])
def export_mutations(rtype, rids, thresh_type, thresh_value,
                     only_sample_rows=None):
    session = scoped_session(session_factory)()

    assert rtype in ('sample', 'clone')
    assert thresh_type in ('seqs', 'percent')

    rids = _split(rids)
    if rtype == 'sample':
        only_sample_rows = _get_arg('only_sample_rows', False) == 'true'
    else:
        only_sample_rows = False

    if rtype == 'sample':
        query = session.query(
            distinct(CloneStats.clone_id).label('clone_id')
        ).filter(
            CloneStats.sample_id.in_(rids)
        )
        clone_ids = map(lambda r: r.clone_id, query.all())
    else:
        clone_ids = rids

    export = MutationExporter(
        session, clone_ids,
        rids if only_sample_rows else None,
        thresh_type, thresh_value
    )
    session.close()

    name = 'mutations_{}.csv'.format(
        time.strftime('%Y-%m-%d-%H-%M'))
    response.headers['Content-Disposition'] = 'attachment;filename={}'.format(
        name)
    for line in export.get_data():
        yield line


@route('/api/data/clone_overlap/<filter_type>/<samples>')
@route('/api/data/subject_clones/<filter_type>/<subject:int>')
def export_clone_overlap(filter_type, samples=None, subject=None):
    session = scoped_session(session_factory)()
    if samples is not None:
        sids = _split(samples)
    ctype = 'samples' if samples is not None else 'subject'

    clones = queries.get_clone_overlap(
        session, filter_type, ctype,
        sids if samples is not None else subject, _get_paging())
    session.close()


def run_rest_service(session_maker, args):
    """Runs the rest service based on command line arguments"""
    global session_factory, rf_bin
    session_factory = session_maker
    rf_bin = args.rarefaction_bin

    bottle.install(EnableCors())
    if args.debug:
        bottle.run(host='0.0.0.0', port=args.port, server='gevent',
                   debug=True)
    else:
        bottle.run(host='0.0.0.0', port=args.port, server='gevent')
