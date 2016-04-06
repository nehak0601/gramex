import tornado.web
import tornado.gen
from .basehandler import BaseHandler
from ..transforms import build_transform


class FunctionHandler(BaseHandler):
    '''
    Renders the output of a function when the URL is called via GET or POST. It
    accepts these parameters when initialized:

    :arg string function: a string that resolves into any Python function or
        method (e.g. ``str.lower``). By default, it is called as
        ``function(handler)`` where handler is this RequestHandler, but you can
        override ``args`` and ``kwargs`` below to replace it with other
        parameters. The result is rendered as-is (and hence must be a string, or
        a Future that resolves to a string.) You can also yield one or more
        results. These are written immediately, in order.
    :arg list args: positional arguments to be passed to the function.
    :arg dict kwargs: keyword arguments to be passed to the function.
    :arg dict headers: HTTP headers to set on the response.
    :arg string redirect: URL to redirect to when the result is done. Used to
        trigger calculations without displaying any output.
    '''
    def initialize(self, headers={}, redirect=None, **kwargs):
        self.params = kwargs
        self.function = build_transform(kwargs, vars={'handler': None})
        self.headers = headers
        self.redirect_url = redirect
        self.post = self.get

    @tornado.web.authenticated
    @tornado.gen.coroutine
    def get(self, *path_args):
        result = self.function(handler=self)
        for header_name, header_value in self.headers.items():
            self.set_header(header_name, header_value)
        if self.redirect_url is not None:
            self.redirect(self.redirect_url or self.request.headers.get('Referer', '/'))
        else:
            # build_transform results are iterable. Loop through each item
            for item in result:
                # Resolve futures and write the result immediately
                if tornado.concurrent.is_future(item):
                    item = yield item
                self.write(item)
                self.flush()