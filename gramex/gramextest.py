import jmespath
import json
import os
import pytest
import re
import requests
import time
import gramex.cache
from fnmatch import fnmatch
from lxml.html import document_fromstring
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from six import string_types
from tornado.web import create_signed_value
from gramex.config import ChainConfig, PathConfig, objectpath, variables

# Get Gramex conf from current directory
gramex_conf = ChainConfig()
gramex_conf['source'] = PathConfig(os.path.join(variables['GRAMEXPATH'], 'gramex.yaml'))
gramex_conf['base'] = PathConfig('gramex.yaml')
secret = objectpath(+gramex_conf, 'app.settings.cookie_secret')
drivers = {}
default = object()


class ChromeConf(dict):
    def __init__(self, **conf):
        self['goog:chromeOptions'] = {'args': ['--no-sandbox']}
        for key, val in conf.items():
            getattr(self, key)(val)

    def headless(self, val):
        if val:
            self['goog:chromeOptions']['args'].append('--headless')


class FirefoxConf(dict):
    def __init__(self, **conf):
        self['moz:firefoxOptions'] = {'args': []}
        for key, val in conf.items():
            getattr(self, key)(val)

    def headless(self, val):
        if val:
            self['moz:firefoxOptions']['args'].append('-headless')


def pytest_collect_file(parent, path):
    # This plugin parses gramextest*.yaml files
    if fnmatch(path.basename, 'gramextest*.yaml'):
        return YamlFile(path, parent)


def pytest_runtest_teardown(item, nextitem):
    if nextitem is None:
        for browser, driver in drivers.items():
            driver.close()


class YamlFile(pytest.File):
    '''
    Generates 1 test case for each test: item in the YAML file.
    '''
    def collect(self):
        # TODO: report error if YAML is invalid
        conf = gramex.cache.open(str(self.fspath), 'config')
        # TODO: create browser only when running test
        for browser, kwargs in conf.get('browsers', {}).items():
            if kwargs in (False, None):
                continue
            kwargs = kwargs if isinstance(kwargs, dict) else {}
            capabilities = globals().get(browser + 'Conf')(**kwargs)
            drivers[browser] = getattr(webdriver, browser)(desired_capabilities=capabilities)
        # TODO: improve naming so that we can use pytest -k
        for index, actions in enumerate(conf.get('urltest', [])):
            name = 'urltest:{}'.format(index + 1)
            yield YamlItem(name, self, actions, URLTest())
        for index, actions in enumerate(conf.get('uitest', [])):
            for browser in drivers:
                name = 'uitest:{}:{}'.format(browser, index + 1)
                yield YamlItem(name, self, actions, UITest(browser))


class YamlItem(pytest.Item):
    def __init__(self, name, parent, actions, registry):
        super(YamlItem, self).__init__(name, parent)
        if isinstance(actions, string_types):
            actions = {actions: {}}
        self.run = []
        for action, options in actions.items():
            # PY3: cmd, *arg = action.strip().split(maxsplit=1)
            parts = action.strip().split(None, 1)
            cmd, arg = (parts[0], parts[1:]) if len(parts) > 1 else (parts[0], [])
            method = getattr(registry, cmd, None)
            if method is None:
                raise ConfError('ERROR: Unknown action: {}'.format(action))
            if isinstance(options, dict):
                # e.g. fetch: {url: <url>, ...} and fetch <url>: {...}
                self.run.append([method, arg, options])
            elif arg:
                # e.g. fetch url: POST and test <selector>: true
                self.run.append([method, arg + [options], {}])
            else:
                # e.g. fetch: <url> and test: <selector>
                self.run.append([method, [options], {}])

    def runtest(self):
        for method, args, kwargs in self.run:
            method(*args, **kwargs)

    def repr_failure(self, excinfo):
        if isinstance(excinfo.value, ConfError):
            return excinfo.value.args[0]
        else:
            return super(YamlItem, self).repr_failure(excinfo)

    def reportinfo(self):
        return self.fspath, 0, self.name


class ConfError(Exception):
    '''Custom exception for error reporting.'''


class URLTest(object):
    r = None

    def fetch(self, url, method='GET', timeout=10, headers=None, user=None, **kwargs):
        if user:
            if secret is None:
                raise ConfError('Missing gramex.yaml:app.settings.cookie_secret. ' +
                                'Cannot encrypt user')
            user = json.dumps(user, ensure_ascii=True, separators=(',', ':'))
            if headers is None:
                headers = {}
            headers['X-Gramex-User'] = create_signed_value(secret, 'user', user)
        URLTest.r = requests.request(method, url, timeout=timeout, headers=headers, **kwargs)

    def code(self, expected):
        match(self.r.status_code, expected, 'code')

    def text(self, expected):
        match(self.r.text, expected, 'text')

    def headers(self, **headers):
        result = self.r.headers
        for header, expected in headers.items():
            match(result.get(header, None), expected, 'headers', header)

    def json(self, **paths):
        try:
            result = self.r.json()
        except Exception as e:
            raise ConfError('json: invalid. %s\n\n%s' % (e, self.r.text))
        for path, expected in paths.items():
            match(jmespath.search(path, result), expected, 'json', path)

    def html(self, **matches):
        try:
            tree = document_fromstring(self.r.text)
        except Exception as e:
            raise ConfError('html: invalid. %s\n\n%s' % (e, self.r.text))
        for selector, value in matches.items():
            nodes = tree.cssselect(selector)
            if not isinstance(value, dict):
                for node in nodes:
                    match(node.text_content(), value, 'html', selector)
            else:
                for attr, val in value.items():
                    if attr == '.length':
                        match(len(nodes), val, 'html', selector, attr)
                    elif attr == '.text':
                        for node in nodes:
                            match(node.text_content(), val, 'html', selector, attr)
                    else:
                        for node in nodes:
                            match(node.get(attr), val, 'html', selector, attr)


class UITest(object):
    def __init__(self, browser):
        self.browser = browser
        self.driver = drivers[browser]

    def find(self, selector, _text=default, **attrs):
        msg = self.browser + ': find ' + selector
        node = self._get(selector)
        # find selector: true / false / null / text / [has, text]
        if _text is not default:
            if _text is None or _text is False:
                return match(node, _text, msg + ': null')
            elif node is None:
                return ConfError('%s matched no nodes' % selector)
            return match(node.text, _text, msg)
        elif node is None:
            # TODO: Fix all error reporting
            raise ConfError('%s matched no nodes' % selector)
        for key, expected in attrs.items():
            if key == '.length':
                actual = len(self._get(selector, multiple=True))
            elif key.startswith('.'):
                actual = getattr(node, key[1:])
            elif key.startswith(':'):
                actual = node.get_property(key[1:])
            else:
                actual = node.get_attribute(key)
            match(actual, expected, msg, key)

    def fetch(self, url):
        self.driver.get(url)

    def back(self, count=1):
        for index in range(count):
            self.driver.back()

    def forward(self, count=1):
        for index in range(count):
            self.driver.forward()

    def wait(self, seconds):
        time.sleep(float(seconds))

    def click(self, selector):
        self._get(selector, must_exist=True).click()

    def scroll(self, selector):
        self.driver.execute_script('arguments[0].scrollIntoView()',
                                   self._get(selector, must_exist=True))

    def script(self, script=None, **scripts):
        # script: alert(1)
        if isinstance(script, string_types):
            script = [script]
        # script: [...]
        if isinstance(script, list):
            for code in script:
                # script: [{window.x: 1}]
                if isinstance(code, dict):
                    self._script(code)
                # script: [window.x=1]
                elif isinstance(code, string_types):
                    self.driver.execute_script(code)
                else:
                    raise ConfError('Cannot run script: %r' % code)
        # script: {window.x: 1}
        self._script(scripts)

    def _script(self, scripts):
        for code, expected in scripts.items():
            msg = self.browser + ': script ' + code
            match(self.driver.execute_script(code), expected, msg)

    def type(self, selector, text):
        self._get(selector, must_exist=True).send_keys(text)

    def screenshot():
        pass

    def submit():
        pass

    def clear():
        pass

    select_method = {
        ('css', True): 'find_elements_by_css_selector',
        ('css', False): 'find_element_by_css_selector',
        ('xpath', True): 'find_elements_by_xpath',
        ('xpath', False): 'find_element_by_xpath',
    }

    def _get(self, selector, multiple=False, must_exist=False):
        engine = 'css'
        if selector.startswith('xpath '):
            engine, selector = selector.split(None, 1)
        try:
            return getattr(self.driver, self.select_method[engine, multiple])(selector)
        except NoSuchElementException:
            if must_exist:
                raise ConfError('No element: ' + selector)
            else:
                return None


def add_operator(grouping, *args):
    method = args[-1]
    for name in args[:-1]:
        operators[name] = (grouping, method)


def eq(condition, msg):
    if not condition:
        raise ConfError(msg)


def norm(s):
    return s.strip().lower()


def match_operator(actual, expected, msg):
    if expected[0] in operators:
        grouping, method = operators[expected[0]]
        eq(grouping(method(actual, value) for value in expected[1:]),
           msg + ' '.join([repr(actual), expected[0], repr(expected[1:])]))
    else:
        raise ConfError(msg + ' unknown operator: %s' % expected)


def match(actual, expected, *msg):
    msg = 'FAIL: ' + '.'.join(msg) + ': '
    err = lambda s: msg + s.format(a=actual, e=expected)   # noqa
    if not isinstance(actual, scalar):
        eq(actual == expected, err('{a!r} == {e!r}'))
    elif expected is None:
        eq(actual is None, err('{a!r} is None'))
    elif expected is True:
        eq(actual, err('{a!r} is truthy'))
    elif expected is False:
        eq(not actual, err('{a!r} is falsey'))
    elif isinstance(expected, scalar):
        eq(actual == expected, err('{a!r} == {e!r}'))
    elif isinstance(expected, list):
        if len(expected):
            if isinstance(expected[0], scalar):
                match_operator(actual, expected, msg)
            elif isinstance(expected[0], list):
                for item in expected:
                    match_operator(actual, item, msg)
            else:
                raise ConfError(err('cannot compare {a!r} with {e!r}'))
        else:
            raise ConfError(err('cannot compare {a!r} with {e!r}'))
    else:
        raise ConfError(err('cannot compare {a!r} with {e!r}'))


def case_insensitive_eq(a, e):
    s = isinstance(a, string_types) and isinstance(e, string_types)
    return norm(e) == norm(a) if s else e == a


def case_insensitive_ne(a, e):
    s = isinstance(a, string_types) and isinstance(e, string_types)
    return norm(e) != norm(a) if s else e != a


scalar = (int, float) + string_types
operators = {}
add_operator(any, 'equal', 'equals', 'is', case_insensitive_eq)
add_operator(any, 'EQUAL', 'EQUALS', 'IS', lambda a, e: e == a)
add_operator(any, 'has', 'in', 'is in', lambda a, e: norm(e) in norm(a))
add_operator(any, 'HAS', 'IN', 'IS IN', lambda a, e: e in a)
add_operator(any, 'regex', 'match', 'matches',
             lambda a, e: re.search(e, a, re.IGNORECASE))
add_operator(any, 'REGEX', 'MATCH', 'MATCHES', lambda a, e: re.search(e, a))
add_operator(any, 'starts with', 'startswith',
             lambda a, e: norm(a).startswith(norm(e)))
add_operator(any, 'STARTS WITH', 'STARTSWITH', lambda a, e: a.startswith(e))
add_operator(any, 'ends with', 'endswith',
             lambda a, e: norm(a).endswith(norm(e)))
add_operator(any, 'ENDS WITH', 'ENDSWITH', lambda a, e: a.endswith(e))
add_operator(all, 'does not equal', 'is not', 'not', 'no', case_insensitive_ne)
add_operator(all, 'DOES NOT EQUAL', 'IS NOT', 'NOT', 'NO', lambda a, e: e != a)
add_operator(all, '>', 'greater than', lambda a, e: a > e)
add_operator(all, '<', 'less than', lambda a, e: a < e)
add_operator(all, '>=', 'greater than or equal to', lambda a, e: a >= e)
add_operator(all, '<=', 'less than or equal to', lambda a, e: a <= e)
add_operator(all, 'has no', 'has not', 'does not have', 'not in', 'is not in',
             lambda a, e: norm(e) not in norm(a))
add_operator(all, 'HAS NO', 'HAS NOT', 'DOES NOT HAVE', 'NOT IN', 'IS NOT IN',
             lambda a, e: e not in a)
add_operator(all, 'does not match',
             lambda a, e: not re.search(e, a, re.IGNORECASE))
add_operator(all, 'DOES NOT MATCH', lambda a, e: not re.search(e, a))
