"""
Views and functions for serving static files. These are only to be used
during development, and SHOULD NOT be used in a production setting.
"""
import mimetypes
import posixpath
import re
from pathlib import Path

from django.http import (
    StreamingHttpResponse, Http404, HttpResponse, HttpResponseNotModified,
)
from django.template import Context, Engine, TemplateDoesNotExist, loader
from django.utils._os import safe_join
from django.utils.http import http_date, parse_http_date
from django.utils.translation import gettext as _, gettext_lazy


def serve(request, path, document_root=None, show_indexes=False):
    """
    Serve static files below a given point in the directory structure.

    To use, put a URL pattern such as::

        from django.views.static import serve

        path('<path:path>', serve, {'document_root': '/path/to/my/files/'})

    in your URLconf. You must provide the ``document_root`` param. You may
    also set ``show_indexes`` to ``True`` if you'd like to serve a basic index
    of the directory.  This index view will use the template hardcoded below,
    but if you'd like to override it, you can create a template called
    ``static/directory_index.html``.
    """
    path = posixpath.normpath(path).lstrip('/')
    fullpath = Path(safe_join(document_root, path))
    if fullpath.is_dir():
        if show_indexes:
            return directory_index(path, fullpath)
        raise Http404(_("Directory indexes are not allowed here."))
    if not fullpath.exists():
        raise Http404(_('“%(path)s” does not exist') % {'path': fullpath})
    # Respect the If-Modified-Since header.
    statobj = fullpath.stat()
    if not was_modified_since(request.META.get('HTTP_IF_MODIFIED_SINCE'),
                              statobj.st_mtime, statobj.st_size):
        return HttpResponseNotModified()
    content_type, encoding = mimetypes.guess_type(str(fullpath))
    content_type = content_type or 'application/octet-stream'
    # response = FileResponse(fullpath.open('rb'), content_type=content_type)

    ranged_file = RangedFileReader(open(fullpath, 'rb'))
    response = StreamingHttpResponse(ranged_file,
                                     content_type=content_type)
    response["Last-Modified"] = http_date(statobj.st_mtime)

    size = statobj.st_size
    response["Content-Length"] = size
    response["Accept-Ranges"] = "bytes"
    # Respect the Range header.
    if "HTTP_RANGE" in request.META:
        try:
            ranges = parse_range_header(request.META['HTTP_RANGE'], size)
        except ValueError:
            ranges = None
        # only handle syntactically valid headers, that are simple (no
        # multipart byteranges)
        if ranges is not None and len(ranges) == 1:
            start, stop = ranges[0]
            if stop > size:
                # requested range not satisfiable
                return HttpResponse(status=416)
            ranged_file.start = start
            ranged_file.stop = stop
            response["Content-Range"] = "bytes %d-%d/%d" % (start, stop - 1, size)
            response["Content-Length"] = stop - start
            response.status_code = 206

    if encoding:
        response["Content-Encoding"] = encoding
    return response


DEFAULT_DIRECTORY_INDEX_TEMPLATE = """
{% load i18n %}
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta http-equiv="Content-type" content="text/html; charset=utf-8">
    <meta http-equiv="Content-Language" content="en-us">
    <meta name="robots" content="NONE,NOARCHIVE">
    <title>{% blocktranslate %}Index of {{ directory }}{% endblocktranslate %}</title>
  </head>
  <body>
    <h1>{% blocktranslate %}Index of {{ directory }}{% endblocktranslate %}</h1>
    <ul>
      {% if directory != "/" %}
      <li><a href="../">../</a></li>
      {% endif %}
      {% for f in file_list %}
      <li><a href="{{ f|urlencode }}">{{ f }}</a></li>
      {% endfor %}
    </ul>
  </body>
</html>
"""
template_translatable = gettext_lazy("Index of %(directory)s")


def directory_index(path, fullpath):
    try:
        t = loader.select_template([
            'static/directory_index.html',
            'static/directory_index',
        ])
    except TemplateDoesNotExist:
        t = Engine(libraries={'i18n': 'django.templatetags.i18n'}).from_string(DEFAULT_DIRECTORY_INDEX_TEMPLATE)
        c = Context()
    else:
        c = {}
    files = []
    for f in fullpath.iterdir():
        if not f.name.startswith('.'):
            url = str(f.relative_to(fullpath))
            if f.is_dir():
                url += '/'
            files.append(url)
    c.update({
        'directory': path + '/',
        'file_list': files,
    })
    return HttpResponse(t.render(c))


def was_modified_since(header=None, mtime=0, size=0):
    """
    Was something modified since the user last downloaded it?

    header
      This is the value of the If-Modified-Since header.  If this is None,
      I'll just return True.

    mtime
      This is the modification time of the item we're talking about.

    size
      This is the size of the item we're talking about.
    """
    try:
        if header is None:
            raise ValueError
        matches = re.match(r"^([^;]+)(; length=([0-9]+))?$", header,
                           re.IGNORECASE)
        header_mtime = parse_http_date(matches[1])
        header_len = matches[3]
        if header_len and int(header_len) != size:
            raise ValueError
        if int(mtime) > header_mtime:
            raise ValueError
    except (AttributeError, ValueError, OverflowError):
        return True
    return False






def parse_range_header(header, resource_size):
    """
    Parses a range header into a list of two-tuples (start, stop) where `start`
    is the starting byte of the range (inclusive) and `stop` is the ending byte
    position of the range (exclusive).
    Returns None if the value of the header is not syntatically valid.
    """
    if not header or '=' not in header:
        return None

    ranges = []
    units, range_ = header.split('=', 1)
    units = units.strip().lower()

    if units != "bytes":
        return None

    for val in range_.split(","):
        val = val.strip()
        if '-' not in val:
            return None

        if val.startswith("-"):
            # suffix-byte-range-spec: this form specifies the last N bytes of an
            # entity-body
            start = resource_size + int(val)
            if start < 0:
                start = 0
            stop = resource_size
        else:
            # byte-range-spec: first-byte-pos "-" [last-byte-pos]
            start, stop = val.split("-", 1)
            start = int(start)
            # the +1 is here since we want the stopping point to be exclusive, whereas in
            # the HTTP spec, the last-byte-pos is inclusive
            stop = int(stop)+1 if stop else resource_size
            if start >= stop:
                return None

        ranges.append((start, stop))

    return ranges





class RangedFileReader:
    """
    Wraps a file like object with an iterator that runs over part (or all) of
    the file defined by start and stop. Blocks of block_size will be returned
    from the starting position, up to, but not including the stop point.
    """
    block_size = 8192
    def __init__(self, file_like, start=0, stop=float("inf"), block_size=None):
        self.f = file_like
        self.block_size = block_size or RangedFileReader.block_size
        self.start = start
        self.stop = stop

    def __iter__(self):
        self.f.seek(self.start)
        position = self.start
        while position < self.stop:
            data = self.f.read(min(self.block_size, self.stop - position))
            if not data:
                break

            yield data
            position += self.block_size