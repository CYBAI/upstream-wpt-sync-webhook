import copy
import json
import os
import requests
import sys
import subprocess
import time
import traceback

API = "https://api.github.com/"
UPSTREAMABLE_PATH = 'tests/wpt/web-platform-tests/'
NO_SYNC_SIGNAL = '[no-wpt-sync]'

def upstream_pulls(config):
    return API + ("repos/%s/web-platform-tests/pulls" % config['upstream_org'])

class Step:
    def __init__(self, name):
        self.name = name

    def provides(self):
        return {}

    def run(self, dry_run):
        pass


class AsyncValue:
    _value = None

    def resolve(self, value):
        self._value = value

    def value(self):
        assert(self._value != None)
        return self._value


def authenticated(config, method, url, json=None):
    s = requests.Session()
    if not method:
        method = 'GET'
    s.headers = {
        'Authorization': 'token %s' % config['token'],
        'User-Agent': 'Servo web-platform-test sync service',
    }
    print('fetching %s' % url)
    response = s.request(method, url, json=json)
    if int(response.status_code / 100) != 2:
        raise ValueError('got unexpected %d response: %s' % (response.status_code, response.text))
    return response


def git(*args, **kwargs):
    command_line = ["git"] + list(*args)
    print(' '.join(map(lambda x: ('"%s"' % x) if ' ' in x else x, command_line)))
    return subprocess.check_output(command_line, cwd=kwargs['cwd'])


class UpstreamStep(Step):
    def __init__(self, servo_pr_number, commits):
        Step.__init__(self, 'UpstreamStep')
        self.servo_pr_number = servo_pr_number
        self.commits = commits

    def provides(self):
        self.branch = AsyncValue()
        return {'branch': self.branch}

    def run(self, config, dry_run):
        branch = _upstream(config, self.servo_pr_number, self.commits.value(), dry_run)
        self.branch.resolve(branch)


def upstream(servo_pr_number, commits, steps):
    step = UpstreamStep(servo_pr_number, commits)
    steps += [step]
    return step.provides()['branch']

def _upstream(config, servo_pr_number, commits, dry_run):
    BRANCH_NAME = "servo_export_%s" % servo_pr_number
    if dry_run:
        return BRANCH_NAME

    def upstream_inner(config, commits):
        PATCH_FILE = 'tmp.patch'
        STRIP_COUNT = UPSTREAMABLE_PATH.count('/') + 1

        # Ensure shallow WPT clone is up to date.
        git(["checkout", "master"], cwd=config['wpt_path'])
        git(["fetch", "origin", "master", "--depth", "1"], cwd=config['wpt_path'])
        git(["reset", "--hard", "origin/master"], cwd=config['wpt_path'])

        # Create a new branch with a unique name that is consistent between updates of the same PR
        git(["checkout", "-b", BRANCH_NAME], cwd=config['wpt_path'])

        patch_path = os.path.join(config['wpt_path'], PATCH_FILE)

        for commit in commits:
            # Export the current diff to a file
            with open(patch_path, 'w') as f:
                f.write(commit['diff'])

            # Remove all non-WPT changes from the diff.
            filtered = subprocess.check_output(["filterdiff",
                                                "-p", "1",
                                                "-i", UPSTREAMABLE_PATH + "*",
                                                PATCH_FILE],
                                               cwd=config['wpt_path'])
            with open(patch_path, 'w') as f:
                f.write(filtered.decode("utf-8"))

            # Apply the filtered changes
            git(["apply", PATCH_FILE, "-p", str(STRIP_COUNT)], cwd=config['wpt_path'])

            # Ensure the patch file is not added with the other changes.
            os.remove(patch_path)

            # Commit the changes
            git(["add", "--all"], cwd=config['wpt_path'])
            git(["commit", "--message", commit['message'], "--author", commit['author']],
                cwd=config['wpt_path'])

        remote_url = "https://{user}:{token}@github.com/{user}/web-platform-tests.git".format(
            user=config['username'],
            token=config['token'],
        )

        # Push the branch upstream (forcing to overwrite any existing changes)
        git(["push", "-f", remote_url, BRANCH_NAME], cwd=config['wpt_path'])
        return BRANCH_NAME

    try:
        return upstream_inner(commits)
    except Exception as e:
        raise e
    finally:
        try:
            git(["checkout", "master"], cwd=config['wpt_path'])
            git(["branch", "-D", BRANCH_NAME], cwd=config['wpt_path'])
        except:
            pass


class ChangeUpstreamStep(Step):
    def __init__(self, upstream, state):
        Step.__init__(self, 'ChangeUpstreamStep')
        self.upstream = upstream
        self.state = state

    def run(self, config, dry_run):
        _change_upstream_pr(config, self.upstream, self.state, dry_run)


def change_upstream_pr(upstream, state, steps):
    steps += [ChangeUpstreamStep(upstream, state)]

def _change_upstream_pr(config, upstream, state, dry_run):
    if dry_run:
        return

    data = {
        'state': state
    }
    return authenticated(config,
                         'PATCH',
                         upstream_pulls(config) + '/' + str(upstream),
                         json=data)


class MergeUpstreamStep(Step):
    def __init__(self, upstream):
        Step.__init__(self, 'MergeUpstreamStep')
        self.upstream = upstream

    def run(self, config, dry_run):
        _merge_upstream_pr(config, self.upstream, dry_run)


def merge_upstream_pr(upstream, steps):
    steps += [MergeUpstreamStep(upstream)]

def _merge_upstream_pr(config, upstream, dry_run):
    if dry_run:
        return

    modify_upstream_pr_labels(config, 'DELETE', ['do not merge yet'], upstream)
    data = {
        'merge_method': 'merge',
    }
    return authenticated(config,
                         'PUT',
                         upstream_pulls(config) + '/' + str(upstream) + '/merge',
                         json=data)


def modify_upstream_pr_labels(config, method, labels, pr_number):
    authenticated(config,
                  method,
                  API + ('repos/%s/web-platform-tests/issues/%s/labels' %
                         (config['upstream_org'], pr_number)),
                  json=labels)


class OpenUpstreamStep(Step):
    def __init__(self, pr_db, pr_number, title, source_org, branch, body):
        Step.__init__(self, 'OpenUpstreamStep')
        self.pr_db = pr_db
        self.pr_number = pr_number
        self.title = title
        self.source_org = source_org
        self.branch = branch
        self.body = body

    def provides(self):
        self.new_pr_url = AsyncValue()
        return {'pr_url': self.new_pr_url}

    def run(self, config, dry_run):
        pr_url = _open_upstream_pr(config,
                                   self.pr_db,
                                   self.pr_number,
                                   self.title,
                                   self.source_org,
                                   self.branch.value(),
                                   self.body,
                                   dry_run)
        self.new_pr_url.resolve(pr_url)


def open_upstream_pr(pr_db, pr_number, title, source_org, branch, body, steps):
    step = OpenUpstreamStep(pr_db, pr_number, title, source_org, branch, body)
    steps += [step]
    return step.provides()['pr_url']

def _open_upstream_pr(config, pr_db, pr_number, title, source_org, branch, body, dry_run):
    if dry_run:
        return 'http://test.url'

    data = {
        'title': title,
        'head': (config['username'] + ':' + branch) if source_org != config['upstream_org'] else branch,
        'base': 'master',
        'body': body,
        'maintainer_can_modify': False,
    }
    r = authenticated(config,
                      'POST',
                      upstream_pulls(config),
                      json=data)
    result = r.json()
    pr_db[pr_number] = result["number"]
    pr_url = result["html_url"]
    modify_upstream_pr_labels(config, 'POST', ['servo-export', 'do not merge yet'], pr_db[pr_number])
    return pr_url


class CommentStep(Step):
    def __init__(self, pr_number, upstream_url):
        Step.__init__(self, 'CommentStep')
        self.pr_number = pr_number
        self.upstream_url = upstream_url

    def run(self, config, dry_run):
        upstream_url = self.upstream_url.value() if isinstance(self.upstream_url, AsyncValue) else self.upstream_url
        _comment_on_pr(config, self.pr_number, upstream_url, dry_run)


def comment_on_pr(pr_number, upstream_url, steps):
    step = CommentStep(pr_number, upstream_url)
    steps += [step]


def _do_comment_on_pr(config, pr_number, body):
    data = {
        'body': body,
    }
    return authenticated(config,
                         'POST',
                         API + ('repos/%s/servo/issues/%s/comments' % (config['servo_org'], pr_number)),
                         json=data)


def _comment_on_pr(config, pr_number, upstream_url, dry_run):
    if dry_run:
        return

    return _do_comment_on_pr(config, pr_number,
                             'Completed upstream sync of web-platform-test changes at %s.' % upstream_url)


def patch_contains_upstreamable_changes(patch_contents):
    for line in patch_contents.splitlines():
        if line.startswith("diff --git") and UPSTREAMABLE_PATH in line:
            return True
    return False


class FetchUpstreamableStep(Step):
    def __init__(self, pull_request):
        Step.__init__(self, 'FetchUpstreamableStep')
        self.pull_request = pull_request

    def provides(self):
        self.commits = AsyncValue()
        return {'commits': self.commits}

    def run(self, config, dry_run):
        commits = _fetch_upstreamable_commits(config, self.pull_request, dry_run)
        self.commits.resolve(commits)


def fetch_upstreamable_commits(pull_request, steps):
    step = FetchUpstreamableStep(pull_request)
    steps += [step]
    return step.provides()['commits']

def _fetch_upstreamable_commits(config, pull_request, dry_run):
    if dry_run:
        return []

    r = authenticated(config, 'GET', pull_request["commits_url"])
    commit_data = r.json()
    filtered_commits = []
    for commit in commit_data:
        r = authenticated(config, 'GET', commit['url'])
        commit_body = r.json()
        for file in commit_body['files']:
            if UPSTREAMABLE_PATH in file['filename']:
                # Retrieve the diff of this commit.
                r = requests.get(commit['html_url'] + '.diff')
                # Create an object that contains everything necessary to transplant this
                # commit to another repository.
                filtered_commits += [{
                    'author': "%s <%s>" % (commit['commit']['author']['name'],
                                           commit['commit']['author']['email']),
                    'message': commit['commit']['message'],
                    'diff': r.text,
                }]
                break
    return filtered_commits


SERVO_PR_URL = "https://github.com/%s/servo/pulls/%s"

def process_new_pr_contents(config, pr_db, pull_request, pr_diff, steps):
    pr_number = str(pull_request['number'])
    # Is this updating an existing pull request?
    if pr_number in pr_db:
        if patch_contains_upstreamable_changes(pr_diff):
            # Retrieve the set of commits that need to be transplanted.
            commits = fetch_upstreamable_commits(pull_request, steps)
            # Push the relevant changes to the upstream branch.
            upstream(pr_number, commits, steps)
            # In case this is adding new upstreamable changes to a PR that was closed
            # due to a lack of upstreamable changes, force it to be reopened.
            change_upstream_pr(pr_db[pr_number], 'opened', steps)
        else:
            # Close the upstream PR, since would contain no changes otherwise.
            change_upstream_pr(pr_db[pr_number], 'closed', steps)
        comment_on_pr(pr_number, upstream_pulls(config) + '/' + str(pr_db[pr_number]), steps)
    elif patch_contains_upstreamable_changes(pr_diff):
        # Retrieve the set of commits that need to be transplanted.
        commits = fetch_upstreamable_commits(pull_request, steps)
        # Push the relevant changes to a new upstream branch.
        branch = upstream(pr_number, commits, steps)
        # TODO: extract the non-checklist/reviewable parts of the pull request body
        #       and add it to the upstream body.
        body = "Reviewed in %s." % (SERVO_PR_URL % (config['servo_org'], pr_number))
        # Create a pull request against the upstream repository for the new branch.
        upstream_url = open_upstream_pr(pr_db, pr_number, pull_request['title'], config['username'], branch, body, steps)
        # Leave a comment to the new pull request in the original pull request.
        comment_on_pr(pr_number, upstream_url, steps)


def process_closed_pr(pr_db, pull_request, steps):
    pr_number = str(pull_request['number'])
    if not pr_number in pr_db:
        # If we don't recognize this PR, it never contained upstreamable changes.
        return
    if pull_request['merged']:
        # Since the upstreamable changes have now been merged locally, merge the
        # corresponding upstream PR.
        merge_upstream_pr(pr_db[pr_number], steps)
        pr_db.pop(pr_number)
    else:
        # If a PR with upstreamable changes is closed without being merged, we
        # don't want to merge the changes upstream either.
        change_upstream_pr(pr_db[pr_number], 'closed', steps)

        
def process_json_payload(config, pr_db, payload, diff_provider):
    pull_request = payload['pull_request']
    if NO_SYNC_SIGNAL in pull_request['body']:
        return []

    steps = []
    if payload['action'] in ['opened', 'synchronize']:
        process_new_pr_contents(config, pr_db, pull_request, diff_provider(pull_request), steps)
    elif payload['action'] == 'closed':
        process_closed_pr(pr_db, pull_request, steps)
    return steps


def save_snapshot(payload, exception_info, pr_db, diff_provider):
    name = 'error-snapshot-%s' % int(round(time.time() * 1000))
    os.mkdir(name)
    with open(os.path.join(name, 'payload.json'), 'w') as f:
        f.write(json.dumps(payload, indent=2))
    with open(os.path.join(name, 'pr_db.json'), 'w') as f:
        f.write(json.dumps(pr_db, indent=2))
    with open(os.path.join(name, 'exception'), 'w') as f:
        f.write(''.join(exception_info))
    with open(os.path.join(name, 'pr.diff'), 'w') as f:
        f.write(diff_provider(payload['pull_request']))
    return name


def process_and_run_steps(config, pr_db, payload, provider, dry_run,
                          step_callback=None, error_callback=None):
    orig_pr_db = copy.deepcopy(pr_db)
    try:
        steps = process_json_payload(config, pr_db, payload, provider)
        for step in steps:
            if step_callback:
                step_callback(step)
            step.run(config, dry_run)
        return True
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        info = traceback.format_exception(exc_type, exc_value, exc_traceback)
        dir_name = save_snapshot(payload, info, orig_pr_db, provider)
        if error_callback:
            error_callback(dir_name)
        return False