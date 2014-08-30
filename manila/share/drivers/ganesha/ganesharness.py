# Copyright (c) 2014 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import cStringIO as StringIO
import json
import os
import pipes
import re
import sys

from manila import exception
from manila.share.drivers.ganesha import utils as ganeshautils


IWIDTH = 2

def _conf2json(cf):
    """Convert Ganesha config to JSON."""
    a = [StringIO.StringIO()]
    state = {'in_quote': False,
             'in_comment': False,
             'escape': False}
    def set_escape():
        state['escape'] = True
    def start_new():
        a.append(StringIO.StringIO())
    cbk = []
    for c in cf:
        if state['in_quote']:
            if not state['escape']:
                if c == '"':
                    state['in_quote'] = False
                    cbk.append(start_new)
                elif c == '\\':
                    cbk.append(set_escape)
        else:
            if c == "#":
                state['in_comment'] = True
            if state['in_comment']:
                if c == "\n":
                    state['in_comment'] = False
            else:
                if c == '"':
                    a.append(StringIO.StringIO())
                    state['in_quote'] = True
        state['escape'] = False
        if not state['in_comment']:
            a[-1].write(c)
        while cbk:
            cbk.pop(0)()

    if state['in_quote']:
        raise RuntimeError("unterminated quoted string")

    # jsonify tokens
    ta = ["{"]
    for w in a:
        w = w.getvalue()

        if w[0] == '"':
            ta.append(w)
            continue

        for pat, s in [# add omitted "=" signs to block openings
                       ('([^=\s])\s*{', '\\1={'),
                       # delete trailing semicolons in blocks
                       (';\s*}', '}'),
                       # add omitted semicolons after blocks
                       ('}\s*([^}\s])', '};\\1'),
                       # separate syntactically significant characters
                       ('([;{}=])', ' \\1 ')]:
            w = re.sub(pat, s, w)

        # map tokens to JSON equivalents
        for wx in w.split():
            if wx == "=":
                wx = ":"
            elif wx  == ";":
                wx = ','
            elif wx in ['{','}'] or re.search('\A-?[1-9]\d*(\.\d+)?\Z', wx):
                pass
            else:
                wx = json.dumps(wx)
            ta.append(wx)
    ta.append("}")

    # group quouted strings
    aa = []
    for w in ta:
        if w[0] == '"':
            if not (aa and isinstance(aa[-1], list)):
                aa.append([])
            aa[-1].append(w)
        else:
            aa.append(w)

    # process quoted string groups by joining them
    aaa = []
    for x in aa:
        if isinstance(x, list):
            x = ''.join(['"'] + [ w[1:-1] for w in x ] + ['"'])
        aaa.append(x)

    return ''.join(aaa)

def _dump_to_conf(cfdict, out=sys.stdout, indent=0):
    if isinstance(cfdict, dict):
        for k,v in cfdict.iteritems():
            if v is None:
                continue
            out.write(' ' * (indent * IWIDTH) +  k + ' ')
            if isinstance(v, dict):
                out.write("{\n")
                _dump_to_conf(v, out, indent + 1)
                out.write(' ' * (indent * IWIDTH) + '}')
            else:
                out.write('= ')
                _dump_to_conf(v, out, indent)
                out.write(';')
            out.write('\n')
    else:
        dj = json.dumps(cfdict)
        if cfdict == dj[1:-1]:
            out.write(cfdict)
        else:
            out.write(dj)

def parseconf(cf):
    """Parse Ganesha config."""
    try:
        # allow config to be specified in JSON --
        # for sake of people who might feel Ganesha config foreign.
        d = json.loads(cf)
    except ValueError:
        d = json.loads(_conf2json(cf))
    return d

def mkconf(cfdict):
    """Create Ganesha config string from cfdict."""
    s = StringIO.StringIO()
    _dump_to_conf(cfdict, s)
    return s.getvalue()

class GanesHarness(object):
    """Ganesha instrumentation classs."""

    def __init__(self, execute, **kwargs):
        self.ganesha_config_path = kwargs['ganesha_config_path']
        self.execute = execute
        execute('mkdir', '-p', self._ganesha_export_dir)
        self.ganesha_db_path = kwargs['ganesha_db_path']
        execute('mkdir', '-p', os.path.dirname(self.ganesha_db_path))
        try:
            execute("sqlite3", self.ganesha_db_path,
              'create table ganesha(key varchar(20) primary key, value int);'
              'insert into ganesha values("exportid", 100);', run_as_root=False)
        except exception.ProcessExecutionError as e:
            if e.stderr != 'Error: table ganesha already exists\n':
                raise

    @property
    def _ganesha_export_dir(self):
        """The directory in which we keep the export specs."""
        return os.path.join(os.path.dirname(self.ganesha_config_path),
                            "export.d")

    def _getpath(self, name):
        """Get the path of config file for name."""
        return os.path.join(self._ganesha_export_dir, name + ".conf")

    def _write_file(self, path, data):
        """Write data to path atomically."""
        dirpath, fname = ( getattr(os.path, q + "name")(path) for q in ("dir", "base") )
        tmpf = self.execute('mktemp', '-p', dirpath, "-t", fname + ".XXXXXX")[0][:-1]
        self.execute('sh', '-c', 'cat > ' + pipes.quote(tmpf), process_input=data)
        self.execute('mv', tmpf, path)

    def _write_conf_file(self, name, data):
        """Write data to config file for name atomically."""
        path = self._getpath(name)
        self._write_file(path, data)
        return path

    confrx = re.compile('\.conf\Z')

    def _mkindex(self):
        """Generate the index file for current exports."""
        files = filter(lambda f: self.confrx.search(f) and f != "INDEX.conf",
                  self.execute('ls',
                    self._ganesha_export_dir, run_as_root=False)[0].\
                    split("\n"))
        index = "".join([
            "%include " + os.path.join(self._ganesha_export_dir, f) + "\n" for
            f in files])
        self._write_conf_file("INDEX", index)

    def _read_export_file(self, name):
        """Return the dict of the export identified by name."""
        return parseconf(self.execute("cat", self._getpath(name))[0])

    def _write_export_file(self, name, cfdict):
        """Write cfdict to the export file of name."""
        for k,v in ganeshautils.walk(cfdict):
            if isinstance(v, basestring) and v[0] == '@':
                msg = _("Incomplete export block: "
                        "value %(val)s of attribute %(key)s is a stub.") % \
                        {'key':k, 'val':v}
                raise exception.InvalidParameterValue(err = msg)
        return self._write_conf_file(name, mkconf(cfdict))

    def _rm_export_file(self, name):
        """Remove export file of name."""
        self.execute("rm", self._getpath(name))

    def _dbus_send_ganesha(self, method, *args, **kwargs):
        """Send a message to Ganesha via dbus."""
        service = kwargs.pop("service", "exportmgr")
        self.execute("dbus-send", "--print-reply", "--system",
          "--dest=org.ganesha.nfsd", "/org/ganesha/nfsd/ExportMgr",
          "org.ganesha.nfsd.%s.%s" % (service, method), *args, **kwargs)

    def _remove_export_dbus(self, name, xid):
        """Remove an export from Ganesha runtime with given export id."""
        self._dbus_send_ganesha("RemoveExport", "uint16:%d" % xid)

    def add_export(self, name, cfdict):
        """Add an export to Ganesha specified by cfdict."""
        xid = cfdict["EXPORT"]["Export_Id"]
        undos = []
        try:
            path = self._write_export_file(name, cfdict)
            undos.append(lambda: self._rm_export_file(name))

            self._dbus_send_ganesha("AddExport", "string:" + path,
              "string:EXPORT(Export_Id=%d)" % xid)
            undos.append(lambda: self._remove_export_dbus(name, xid))

            self._mkindex()
        except:
            for u in undos:
                u()
            raise

    def remove_export(self, name):
        """Remove an export from Ganesha."""
        try:
            cfdict = self._read_export_file(name)
            self._remove_export_dbus(name, cfdict["EXPORT"]["Export_Id"])
        finally:
            self._rm_export_file(name)
            self._mkindex()

    def get_export_id(self):
        """Get a new export id."""
        # XXX overflowing the export id (16 bit unsigned integer)
        # is not handled
        out = self.execute("sqlite3", self.ganesha_db_path,
           'update ganesha set value = value + 1;'
           'select * from ganesha where key = "exportid";',
           run_as_root=False)[0]
        return int(re.search('\Aexportid\|(\d+)$', out).groups()[0])
