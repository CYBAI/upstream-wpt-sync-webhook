import copy
from functools import partial
import hook
import json
import os
import requests
import sync
from sync import process_and_run_steps, UPSTREAMABLE_PATH
import sys
from test_api_server import start_server
import threading
import time

def wait_for_server(port):
    # Wait for server to finish setting up before continuing
    while True:
        try:
            r = requests.get('http://localhost:' + str(port) + '/ping')
            assert(r.status_code == 200)
            assert(r.text == 'pong')
            break
        except:
            time.sleep(0.5)

class APIServerThread(object):
    def __init__(self, config):
        self.port = 9000
        thread = threading.Thread(target=self.run, args=(config,))
        thread.daemon = True
        thread.start()
        wait_for_server(self.port)

    def run(self, config):
        start_server(self.port, config)

    def shutdown(self):
        r = requests.post('http://localhost:%d/shutdown' % self.port)
        assert(r.status_code == 204)


def get_pr_diff(test, pull_request):
    if 'diff' in test:
        diff_file = test['diff']
    else:
        diff_file = str(pull_request['number']) + '.diff'
    with open(os.path.join('tests', diff_file)) as f:
        return f.read()

with open('tests.json') as f:
    tests = json.loads(f.read())

config = {
    'servo_org': 'servo',
    'username': 'servo-wpt-sync',
    'upstream_org': 'jdm',
    'port': 5000,
    'token': '',
    'api': 'http://localhost:9000',
    'override_host': 'http://localhost:9000',
}

def make_api_config(test, payload):
    is_upstreamable = UPSTREAMABLE_PATH in get_pr_diff(test, payload["pull_request"])
    api_config = {
        "upstreamable_commits": 1 if is_upstreamable else 0,
        "non_upstreamable_commits": 0 if is_upstreamable else 1,
    }
    api_config.update(test.get('api_config', {}))
    return api_config

for test in tests:
    with open(os.path.join('tests', test['payload'])) as f:
        payload = json.loads(f.read())

    server = APIServerThread(make_api_config(test, payload))

    print(test['name'] + ':'),
    executed = []
    def callback(step):
        global executed
        executed += [step.name]
    def error_callback(dir_name):
        #print('saved error snapshot: %s' % dir_name)
        with open(os.path.join(dir_name, "exception")) as f:
            print(f.read())
        import shutil
        shutil.rmtree(dir_name)
    process_and_run_steps(config,
                          test['db'],
                          payload,
                          partial(get_pr_diff, test),
                          True,
                          step_callback=callback,
                          error_callback=error_callback)
    server.shutdown()
    if all(map(lambda (s, s2): s == s2 if ':' not in s2 else s.startswith(s2),
               zip(executed, test['expected']))):
        print('passed')
    else:
        print()
        print(executed)
        print('vs')
        print(test['expected'])
        assert(executed == test['expected'])

class ServerThread(object):
    def __init__(self, config):
        self.port = config['port']
        thread = threading.Thread(target=self.run, args=(config,))
        thread.daemon = True
        thread.start()
        wait_for_server(config['port'])

    def run(self, config):
        hook.main(config, {})

    def shutdown(self):
        r = requests.post('http://localhost:%d/shutdown' % self.port)
        assert(r.status_code == 204)


print('testing server hook with /test')

for (i, test) in enumerate(tests):
    print(test['name'] + ':'),

    this_config = copy.deepcopy(config)
    this_config['port'] += i
    server = ServerThread(this_config)

    with open(os.path.join('tests', test['payload'])) as f:
        payload = f.read()

    api_server = APIServerThread(make_api_config(test, json.loads(payload)))

    r = requests.post('http://localhost:' + str(this_config['port']) + '/test', data={'payload': payload})
    if r.status_code != 204:
        print(r.status_code)
    assert(r.status_code == 204)
    server.shutdown()
    api_server.shutdown()

    print('passed')
