#!/usr/bin/env python

import os
import calendar
from datetime import datetime, timedelta
from itertools import groupby
from collections import Counter
from functools import wraps

import MySQLdb
from flask import Flask, request, json, Response, abort

static_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
app = Flask(__name__, static_url_path="", static_folder=static_path)
app.config.from_pyfile('../config/config.py') 

class CSetSummary(object):
    def __init__(self, cset_id):
        self.cset_id = cset_id
        self.green = Counter()
        self.orange = Counter()
        self.red = Counter()
        self.blue = Counter()


def create_db_connnection():
    return MySQLdb.connect(host=app.config['DB_HOST'],
                           user=app.config['DB_USER'],
                           passwd="",
                           db=app.config['DB_NAME'])


def serialize_to_json(object):
    """Serialize class objects to json"""
    try:
        return object.__dict__
    except AttributeError:
        raise TypeError(repr(object) + 'is not JSON serializable')


def json_response(func):
    """Decorator: Serialize response to json"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        result = json.dumps(func(*args, **kwargs) or {"error": "No data found for your request"},
                            default=serialize_to_json)
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(result)))
        ]
        return Response(result, status=200, headers=headers)

    return wrapper


def get_date_range(dates):
    if dates:
        return {'startDate': min(dates).strftime('%Y-%m-%d %H:%M'),
                'endDate': max(dates).strftime('%Y-%m-%d %H:%M')}


def clean_date_params(query_dict):
    """Parse request date params"""
    now = datetime.now()

    # get dates params
    start_date_param = query_dict.get('startDate') or query_dict.get('startdate')
    end_date_param = query_dict.get('endDate') or query_dict.get('enddate')

    # parse dates
    end_date = (parse_date(end_date_param) or now)
    start_date = parse_date(start_date_param) or end_date - timedelta(days=7)

    # validate dates
    if start_date > now or start_date.date() >= end_date.date():
        start_date = now - timedelta(days=7)
        end_date = now + timedelta(days=1)

    return start_date.date(), end_date.date()


def parse_date(date_):
    if date_ is None:
        return

    masks = ['%Y-%m-%d',
             '%Y-%m-%dT%H:%M',
             '%Y-%m-%d %H:%M']

    for mask in masks:
        try:
            return datetime.strptime(date_, mask)
        except ValueError:
            pass


def calculate_fail_rate(passes, retries, totals):
    # skip calculation for slaves and platform with no failures
    if passes == totals:
        results = [0, 0]

    else:
        results = []
        denominators = [totals - retries, totals]
        for denominator in denominators:
            try:
                result = 100 - (passes * 100) / float(denominator)
            except ZeroDivisionError:
                result = 0
            results.append(round(result, 2))

    return dict(zip(['failRate', 'failRateWithRetries'], results))


def binify(bins, data):
    result = []
    for i, bin in enumerate(bins):
        if i > 0:
            result.append(len(filter(lambda x: x >= bins[i - 1] and x < bin, data)))
        else:
            result.append(len(filter(lambda x: x < bin, data)))

    result.append(len(filter(lambda x: x >= bins[-1], data)))

    return result


@app.route("/data/results/flot/day/")
@json_response
def run_results_day_flot_query():
    """ This function returns the total failures/total jobs data per day for all platforms. It is sending the data in the format required by flot.Flot is a jQuery package used for 'attractive' plotting """

    start_date, end_date = clean_date_params(request.args)

    platforms = ['android4.0', 'android2.3', 'linux32', 'winxp', 'win7', 'win8', 'osx10.6', 'osx10.7', 'osx10.8']
    db = create_db_connnection()

    data_platforms = {}
    for platform in platforms:
        cursor = db.cursor()
        cursor.execute("""select DATE(date) as day,sum(result="%s") as failures,count(*) as totals from testjobs
                          where platform="%s" and date >= "%s" and date <= "%s" group by day""" % ('testfailed', platform, start_date, end_date))

        query_results = cursor.fetchall()

        dates = []
        data = {}
        data['failures'] = []
        data['totals'] = []

        for day, fail, total in query_results:
            dates.append(day)
            timestamp = calendar.timegm(day.timetuple()) * 1000
            data['failures'].append((timestamp, int(fail)))
            data['totals'].append((timestamp, int(total)))

        cursor.close()

        data_platforms[platform] = {'data': data, 'dates': get_date_range(dates)}

    db.close()
    return data_platforms


@app.route("/data/slaves/")
@json_response
def run_slaves_query():
    start_date, end_date = clean_date_params(request.args)

    days_to_show = (end_date - start_date).days
    if days_to_show <= 8:
        jobs = 5
    else:
        jobs = int(round(days_to_show * 0.4))

    info = '''Only slaves with more than %d jobs are displayed.''' % jobs

    db = create_db_connnection()
    cursor = db.cursor()
    cursor.execute("""select slave, result, date from testjobs
                      where result in
                      ("retry", "testfailed", "success", "busted", "exception")
                      and date between "{0}" and "{1}"
                      order by date;""".format(start_date, end_date))

    query_results = cursor.fetchall()
    cursor.close()
    db.close()

    if not query_results:
        return

    data = {}
    labels = 'fail retry infra success total'.split()
    summary = {result: 0 for result in labels}
    summary['jobs_since_last_success'] = 0
    dates = []

    for name, result, date in query_results:
        data.setdefault(name, summary.copy())
        data[name]['jobs_since_last_success'] += 1
        if result == 'testfailed':
            data[name]['fail'] += 1
        elif result == 'retry':
            data[name]['retry'] += 1
        elif result == 'success':
            data[name]['success'] += 1
            data[name]['jobs_since_last_success'] = 0
        elif result == 'busted' or result == 'exception':
            data[name]['infra'] += 1
        data[name]['total'] += 1
        dates.append(date)

    # filter slaves
    slave_list = [slave for slave in data if data[slave]['total'] > jobs]

    # calculate failure rate only for slaves that we're going to display
    for slave in slave_list:
        results = data[slave]
        fail_rates = calculate_fail_rate(results['success'],
                                         results['retry'],
                                         results['total'])
        data[slave]['sfr'] = fail_rates


    platforms = {}

    # group slaves by platform and calculate platform failure rate
    slaves = sorted(data.keys())
    for platform, slave_group in groupby(slaves, lambda x: x.rsplit('-', 1)[0]):
        slaves = list(slave_group)

        # don't calculate failure rate for platform we're not going to show
        if not any(slave in slaves for slave in slave_list):
            continue

        platforms[platform] = {}
        results = {}

        for label in ['success', 'retry', 'total']:
            r = reduce(lambda x, y: x + y,
                       [data[slave][label] for slave in slaves])
            results[label] = r

        fail_rates = calculate_fail_rate(results['success'],
                                         results['retry'],
                                         results['total'])
        platforms[platform].update(fail_rates)

    # remove data that we don't need
    for slave in data.keys():
        if slave not in slave_list:
            del data[slave]

    return {'slaves': data,
            'platforms': platforms,
            'dates': get_date_range(dates),
            'disclaimer': info}


@app.route("/data/platform/")
@json_response
def run_platform_query():
    platform = request.args.get("platform")
    start_date, end_date = clean_date_params(request.args)

    log_message = 'platform: %s startDate: %s endDate: %s' % (platform,
                    start_date.strftime('%Y-%m-%d'),
                    end_date.strftime('%Y-%m-%d'))
    app.logger.debug(log_message)

    db = create_db_connnection()
    cursor = db.cursor()
    cursor.execute("""select distinct revision from testjobs
                      where platform = '%s'
                      and branch = 'mozilla-central'
                      and date between '%s' and '%s'
                      order by date desc;""" % (platform, start_date, end_date))

    csets = cursor.fetchall()

    cset_summaries = []
    test_summaries = {}
    dates = []

    labels = 'green orange blue red'.split()
    summary = {result: 0 for result in labels}

    for cset in csets:
        cset_id = cset[0]
        cset_summary = CSetSummary(cset_id)

        cursor.execute("""select result, testtype, date from testjobs
                          where platform='%s' and buildtype='opt' and revision='%s'
                          order by testtype""" % (platform, cset_id))

        test_results = cursor.fetchall()

        for res, testtype, date in test_results:
            test_summary = test_summaries.setdefault(testtype, summary.copy())

            if res == 'success':
                cset_summary.green[testtype] += 1
                test_summary['green'] += 1
            elif res == 'testfailed':
                cset_summary.orange[testtype] += 1
                test_summary['orange'] += 1
            elif res == 'retry':
                cset_summary.blue[testtype] += 1
                test_summary['blue'] += 1
            elif res == 'exception' or res == 'busted':
                cset_summary.red[testtype] += 1
                test_summary['red'] += 1
            elif res == 'usercancel':
                app.logger.debug('usercancel')
            else:
                app.logger.debug('UNRECOGNIZED RESULT: %s' % result)
            dates.append(date)

        cset_summaries.append(cset_summary)

    cursor.close()
    db.close()

    # sort tests alphabetically and append total & percentage to end of the list
    test_types = sorted(test_summaries.keys())
    test_types += ['total', 'percentage']

    # calculate total stats and percentage
    total = Counter()
    percentage = {}

    for test in test_summaries:
        total.update(test_summaries[test])
    test_count = sum(total.values())

    for key in total:
        percentage[key] = round((100.0 * total[key] / test_count), 2)

    fail_rates = calculate_fail_rate(passes=total['green'],
                                     retries=total['blue'],
                                     totals=test_count)

    test_summaries['total'] = total
    test_summaries['percentage'] = percentage

    return {'testTypes': test_types,
            'byRevision': cset_summaries,
            'byTest': test_summaries,
            'failRates': fail_rates,
            'dates': get_date_range(dates)}


@app.route("/data/seta/")
@json_response
def run_seta_query():
    platforms = request.values.getlist("platform")
    testtype = request.values.getlist("test")
    buildtype = request.values.getlist("build")

    if not platforms:
        platforms = ["linux32", "linux64", "osx10.6", "osx10.8", "winxp", "win7", "win8"]

    if not buildtype:
        buildtype = ["opt", "debug"]

    if not testtype:
        testtype = ["mochitest-1"]

    #force this to an int of either -2, -1, 1, 2
    jobtype = int(request.args.get("jobtype", 1))
    start_date, end_date = clean_date_params(request.args)

    log_message = 'platforms: %s, buildtype: %s, testtype: %s, \
                   jobtype: %s, startDate: %s endDate: %s' % (platforms,
                    buildtype, testtype, jobtype,
                    start_date.strftime('%Y-%m-%d'),
                    end_date.strftime('%Y-%m-%d'))
    app.logger.debug(log_message)

    db = create_db_connnection()
    cursor = db.cursor()
    all_platforms = ' or '.join(["platform='%s'" % a for a in platforms])
    all_tests = ' or '.join(["testtype='%s'" % a for a in testtype])
    all_types = ' or '.join(["buildtype='%s'" % a for a in buildtype])
    query = """select bugid, platform, buildtype, testtype from testjobs
                      where regression=%s
                      and (%s)
                      and (%s)
                      and (%s)
                      and date between '%s' and '%s';""" % \
            (jobtype, all_platforms, all_tests, all_types, start_date, end_date)
    cursor.execute(query)
    failures = {}
    for d in cursor.fetchall():
        if d[0] not in failures:
            failures[d[0]] = []
        failures[d[0]].append([d[1], d[2], d[3]])

    return {'failures': failures}


@app.errorhandler(404)
@json_response
def handler404(error):
    return {"status": 404, "msg": str(error)}

@app.route("/")
def root_directory():
    return template("index.html")

@app.route("/<string:filename>")
def template(filename):
    filename = os.path.join(static_path, filename)
    if os.path.exists(filename):
        with open(filename, 'r') as f:
            response_body = f.read()
        return response_body
    abort(404)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8157, debug=app.config['DEBUG'])
