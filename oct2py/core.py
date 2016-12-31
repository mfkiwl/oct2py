"""
.. module:: core
   :synopsis: Main module for oct2py package.
              Contains the core session object Oct2Py

.. moduleauthor:: Steven Silvester <steven.silvester@ieee.org>

"""
from __future__ import print_function
import os
import atexit
import logging
import signal
import shutil
import time
import tempfile
import types

import numpy as np
from metakernel.pexpect import TIMEOUT, EOF
from octave_kernel.kernel import OctaveEngine, STDIN_PROMPT

from oct2py.matwrite import MatWrite, write_file
from oct2py.matread import MatRead, read_file
from oct2py.utils import (
    get_nout, Oct2PyError, get_log, Struct)
from oct2py.dynamic import _make_octave_class, _make_octave_command
from oct2py.compat import unicode, input, PY2, StringIO


class Oct2Py(object):

    """Manages an Octave session.

    Uses MAT files to pass data between Octave and Numpy.
    The function must either exist as an m-file in this directory or
    on Octave's path.
    The first command will take about 0.5s for Octave to load up.
    The subsequent commands will be much faster.

    You may provide a logger object for logging events, or the oct2py.get_log()
    default will be used.  Events will be logged as debug unless verbose is set
    when calling a command, then they will be logged as info.

    Parameters
    ----------
    executable : str, optional
        Name of the Octave executable, can be a system path.  If this is not
        given, we look for an OCTAVE_EXECUTABLE environmental variable.
        The fallback is to call "octave-cli" or "octave".
    logger : logging object, optional
        Optional logger to use for Oct2Py session
    timeout : float, optional
        Timeout in seconds for commands
    oned_as : {'row', 'column'}, optional
        If 'column', write 1-D numpy arrays as column vectors.
        If 'row', write 1-D numpy arrays as row vectors.}
    temp_dir : str, optional
        If specified, the session's MAT files will be created in the
        directory, otherwise a default directory is used.  This can be
        a shared memory (tmpfs) path.
    convert_to_float : bool, optional
        If true, convert integer types to float when passing to Octave.
    """

    def __init__(self, executable=None, logger=None, timeout=None,
                 oned_as='row', temp_dir=None, convert_to_float=True):
        """Start Octave and set up the session.
        """
        self._oned_as = oned_as
        self._executable = executable

        self.timeout = timeout
        if logger is not None:
            self.logger = logger
        else:
            self.logger = get_log()
        # self.logger.setLevel(logging.DEBUG)
        self._session = None
        self.temp_dir = temp_dir or tempfile.mkdtemp()
        self._convert_to_float = convert_to_float
        self.restart()

    @property
    def convert_to_float(self):
        return self._convert_to_float

    @convert_to_float.setter
    def convert_to_float(self, value):
        self._writer.convert_to_float = value
        self._convert_to_float = value

    def __enter__(self):
        """Return octave object, restart session if necessary"""
        if not self._session:
            self.restart()
        return self

    def __exit__(self, type, value, traceback):
        """Close session"""
        self.exit()

    def exit(self):
        """Quits this octave session and removes temp files
        """
        if self._session:
            self._session.close()
        self._session = None

    def push(self, name, var, verbose=True, timeout=None):
        """
        Put a variable or variables into the Octave session.

        Parameters
        ----------
        name : str or list
            Name of the variable(s).
        var : object or list
            The value(s) to pass.
        timeout : float
            Time to wait for response from Octave (per character).

        Examples
        --------
        >>> from oct2py import octave
        >>> y = [1, 2]
        >>> octave.push('y', y)
        >>> octave.pull('y')
        array([[1, 2]])
        >>> octave.push(['x', 'y'], ['spam', [1, 2, 3, 4]])
        >>> octave.pull(['x', 'y'])  # doctest: +SKIP
        [u'spam', array([[1, 2, 3, 4]])]

        Notes
        -----
        Integer type arguments will be converted to floating point
        unless `convert_to_float=False`.

        """
        if isinstance(name, (str, unicode)):
            name = [name]
            var = [var]

        for (n, v) in zip(name, var):
            self.feval('assignin', 'base', n, v, nout=0, verbose=verbose,
                       timeout=timeout)

    def pull(self, var, verbose=True, timeout=None):
        """
        Retrieve a value or values from the Octave session.

        Parameters
        ----------
        var : str or list
            Name of the variable(s) to retrieve.
        timeout : float
            Time to wait for response from Octave (per character).

        Returns
        -------
        out : object
            Object returned by Octave.

        Raises:
          Oct2PyError
            If the variable does not exist in the Octave session.

        Examples:
          >>> from oct2py import octave
          >>> y = [1, 2]
          >>> octave.push('y', y)
          >>> octave.pull('y')
          array([[1, 2]])
          >>> octave.push(['x', 'y'], ['spam', [1, 2, 3, 4]])
          >>> octave.pull(['x', 'y'])  # doctest: +SKIP
          [u'spam', array([[1, 2, 3, 4]])]

        """
        if isinstance(var, (str, unicode)):
            var = [var]
        vals = [self.feval('evalin', 'base', v, nout=1, verbose=verbose,
                           timeout=timeout) for v in var]
        if len(var) == 1:
            return vals[0]
        return vals

    def extract_figures(self, plot_dir):
        """Extract the figures that were created in the given plot dir.

        Parameters
        ----------
        plot_dir: str
            The plot directory that was used in the call to "eval()".

        Notes
        -----
        This assumes that the figures were created with the specified
        `plot_dir`, e.g. `oc.plot([1,2,3], plot_dir='/tmp/foo').

        Returns
        -------
        out: list
            The IPython Image or SVG objects for the figures.
            These objects have a `.data` attribute with the raw image data,
            and can be used with the `display` function from `IPython` for
            rich display.
        """
        return self._session.extract_figures(plot_dir)

    def set_plot_settings(self, width=None, height=None, format=None,
                          res=None, name=None, dir=None,
                          inline=True):
        pass

    def feval(self, func_path, *func_args, nout=None, verbose=True,
              var_name='', timeout=None, **kwargs):
        """Run a function in Matlab and return the result.

        Parameters
        ----------
        func_path: str
            Name of function to run or a path to an m-file.
        func_args: object, optional
            Args to send to the function.
        nout: int, optional
            Desired number of return arguments.  If not given, the number
            of arguments will be inferred from the return value(s).
        verbose: int, optional
            If False, logs outputs at the DEBUG level instead of INFO.
        var_name: str, optional
            If given, saves the result to the given Octave variable name
            instead of returning it.
        timeout: float, optional
            The timeout in seconds for the call.
        kwargs:
            Keyword arguments are passed to Octave in the form [key, val] so
            that matlab.plot(x, y, '--', LineWidth=2) would be translated into
            plot(x, y, '--', 'LineWidth', 2)

        Returns
        -------
        The Python value(s) returned by the Octave function call.
        """
        if nout is None:
            nout = get_nout() or 1
        func_args += tuple(item for pair in zip(kwargs.keys(), kwargs.values())
                           for item in pair)
        dname = os.path.dirname(func_path)
        fname = os.path.basename(func_path)
        func_name, ext = os.path.splitext(fname)
        if ext and not ext == '.m':
            raise TypeError('Need to give path to .m file')
        return self._eval(func_name, func_args, dname=dname, nout=nout,
                          timeout=timeout, verbose=verbose, var_name=var_name)

    def _eval(self, func_name, func_args, dname='', nout=0,
              timeout=None, verbose=True, var_name=''):
        """Run the given function with the given args.
        """

        # Set up our mat file paths.
        out_file = os.path.join(self.temp_dir, 'writer.mat')
        out_file = out_file.replace(os.path.sep, '/')
        in_file = os.path.join(self.temp_dir, 'reader.mat')
        in_file = in_file.replace(os.path.sep, '/')

        # Save the request data to the output file.
        func_args = np.array(func_args, dtype=object)
        req = dict(func_name=func_name, func_args=func_args,
                   dname=dname, nout=nout, var_name=var_name)
        write_file(req, out_file, oned_as=self._oned_as,
                   convert_to_float=self.convert_to_float)

        # Set up the engine and evaluate the `_pyeval()` function.
        engine = self._session.engine
        if not verbose:
            engine.stream_handler = self.logger.debug
        else:
            engine.stream_handler = self.logger.info
        engine.eval('_pyeval("%s", "%s");' % (out_file, in_file),
                    timeout=timeout)

        # Read in the output.
        resp = read_file(in_file)
        if resp['error']:
            raise Oct2PyError(resp['error']['message'])
        result = resp['result']
        if not str(result):
            result = None
        return result

    def eval(self, cmds, verbose=True, timeout=None, **kwargs):
        """
        Evaluate an Octave command or commands.

        Parameters
        ----------
        cmds : str or list
            Commands(s) to pass to Octave.
        verbose : bool, optional
             Log Octave output at INFO level.  If False, log at DEBUG level.
        timeout : float, optional
            Time to wait for response from Octave (per character).
        **kwargs Deprecated keyword arguments.  Use `set_plot_settings`.

        Returns
        -------
        out : object
            Octave "ans" variable, or None.

        Raises
        ------
        Oct2PyError
            If the command(s) fail.

        """
        if isinstance(cmds, (str, unicode)):
            cmds = [cmds]

        # Handle deprecated `temp_dir` kwarg.
        prev_temp_dir = self.temp_dir
        self.temp_dir = kwargs.get('temp_dir', prev_temp_dir)

        ans = None
        for cmd in cmds:
            ans = self.feval('evalin', 'base', cmd, verbose=verbose,
                             nout=0, timeout=timeout)

        self.temp_dir = prev_temp_dir

        # Handle deprecated `return_both` kwarg.
        if kwargs.get('return_both', False):
            return '', ans

        return ans

    def restart(self):
        """Restart an Octave session in a clean state
        """
        if self._session:
            self._session.close()
        self._reader = MatRead()
        self._writer = MatWrite(self._oned_as,
                                self._convert_to_float)
        self._session = _Session(self._executable, self.logger)

    # --------------------------------------------------------------
    # Private API
    # --------------------------------------------------------------

    def _call(self, func, *inputs, **kwargs):
        """
        Oct2Py Parameters
        --------------------------
        inputs : array_like
            Variables to pass to the function.
        verbose : bool, optional
             Log Octave output at INFO level.  If False, log at DEBUG level.
        nout : int, optional
            Number of output arguments.
            This is set automatically based on the number of return values
            requested.
            You can override this behavior by passing a different value.
        timeout : float, optional
            Time to wait for response from Octave (per character).
        plot_dir: str, optional
            If specificed, save the session's plot figures to the plot
            directory instead of displaying the plot window.
        plot_name : str, optional
            Saved plots will start with `plot_name` and
            end with "_%%.xxx' where %% is the plot number and
            xxx is the `plot_format`.
        plot_format: str, optional
            The format in which to save the plot.
        plot_width: int, optional
            The plot with in pixels.
        plot_height: int, optional
            The plot height in pixels.
        kwargs : dictionary, optional
            Key - value pairs to be passed as prop - value inputs to the
            function.  The values must be strings or numbers.

        Returns
        -----------
        out : value
            Value returned by the function.

        Raises
        ----------
        Oct2PyError
            If the function call is unsucessful.

        Notes
        -----
        Integer type arguments will be converted to floating point
        unless `convert_to_float=False`.

        """
        nout = kwargs.pop('nout', get_nout())
        is_class = kwargs.pop('_is_class', False)
        is_class_lookup = kwargs.pop('_is_class_lookup', False)
        class_var = kwargs.pop('_class_var', '')

        argout_list = ['_']

        # these three lines will form the commands sent to Octave
        # load("-v6", "infile", "invar1", ...)
        # [a, b, c] = foo(A, B, C)
        # save("-v6", "out_file", "outvar1", ...)
        load_line = call_line = save_line = ''

        prop_vals = []
        eval_kwargs = {}
        for (key, value) in kwargs.items():
            if key in ['verbose', 'timeout'] or key.startswith('plot_'):
                eval_kwargs[key] = value
                continue
            if isinstance(value, (str, unicode, int, float)):
                prop_vals.append('"%s", %s' % (key, repr(value)))
            else:
                msg = 'Keyword arguments must be a string or number: '
                msg += '%s = %s' % (key, value)
                raise Oct2PyError(msg)
        prop_vals = ', '.join(prop_vals)

        try:
            temp_dir = tempfile.mkdtemp(dir=self.temp_dir)
            self._reader.create_file(temp_dir)
            if nout:
                # create a dummy list of var names ("a", "b", "c", ...)
                # use ascii char codes so we can increment
                argout_list, save_line = self._reader.setup(nout)
                call_line = '[{0}] = '.format(', '.join(argout_list))

            call_line += func + '('

            if is_class_lookup:
                call_line += '%s' % class_var
                if inputs or prop_vals:
                    call_line += ', '

            if inputs:
                argin_list, load_line = self._writer.create_file(
                    temp_dir, inputs)
                call_line += ', '.join(argin_list)

            if prop_vals:
                if inputs:
                    call_line += ', '
                call_line += prop_vals

            call_line += ');'

            # create the command and execute in octave
            cmd = [load_line, call_line, save_line]

            if is_class:
                cmd.append('%s = %s;' % (class_var, argout_list[0]))
            data = self.eval(cmd, temp_dir=temp_dir, **eval_kwargs)
        finally:
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass

        if isinstance(data, dict) and not isinstance(data, Struct):
            data = [data.get(v, None) for v in argout_list]
            if len(data) == 1 and data.values()[0] is None:
                data = None

        return data

    def _exists(self, name):
        exist = self.eval('exist {0}'.format(name), log=False,
                          verbose=False)
        return exist != 0

    def _get_doc(self, name):
        """
        Get the documentation of an Octave procedure or object.

        Parameters
        ----------
        name : str
            Function name to search for.

        Returns
        -------
        out : str
          Documentation string.

        Raises
        ------
        Oct2PyError
           If the procedure or object does not exist.

        """
        doc = 'No documentation for %s' % name

        try:
            doc, _ = self.eval('help {0}'.format(name), log=False,
                               verbose=False, return_both=True)
        except Oct2PyError as e:
            if 'syntax error' in str(e).lower():
                raise(e)
            doc, _ = self.eval('type("{0}")'.format(name), log=False,
                               verbose=False, return_both=True)
            if isinstance(doc, list):
                doc = doc[0]
            doc = '\n'.join(doc.splitlines()[:3])

        default = self._call.__doc__
        doc += '\n' + '\n'.join([line[8:] for line in default.splitlines()])
        doc = '\n' + doc

        # convert to ascii for pydoc
        try:
            doc = doc.encode('ascii', 'replace').decode('ascii')
        except UnicodeDecodeError as e:
            self.logger.debug(e)

        return doc

    def __getattr__(self, attr):
        """Automatically creates a wapper to an Octave function or object.

        Adapted from the mlabwrap project.

        """
        # needed for help(Oct2Py())
        if attr.startswith('__'):
            return super(Oct2Py, self).__getattr__(attr)

        # close_ -> close
        if attr[-1] == "_":
            name = attr[:-1]
        else:
            name = attr

        # Make sure the name exists.
        if not self._exists(name):
            msg = 'Name: "%s" does not exist on the Octave session path'
            raise Oct2PyError(msg % name)

        # Check for user defined class.
        try:
            # Prevent the debug prompt from coming up.
            if name == 'keyboard':
                isobj = False
            else:
                isobj = self.eval('isobject(%s);' % name) == 1
        except Exception:
            isobj = False

        if isobj:
            obj = _make_octave_class(self, name)
        else:
            obj = _make_octave_command(self, name)
            # bind to the instance.
            if PY2:
                obj = types.MethodType(obj, self, Oct2Py)
            else:
                obj = types.MethodType(obj, self)

        # !!! attr, *not* name, because we might have python keyword name!
        setattr(self, attr, obj)

        return obj


class _Session(object):

    """Low-level session Octave session interaction.
    """

    def __init__(self, executable, logger=None):
        if executable:
            os.environ['OCTAVE_EXECUTABLE'] = executable
        if 'OCTAVE_EXECUTABLE' not in os.environ and 'OCTAVE' in os.environ:
            os.environ['OCTAVE_EXECUTABLE'] = os.environ['OCTAVE']
        self.engine = OctaveEngine(stdin_handler=self._handle_stdin)
        self.proc = self.engine.repl.child
        self.logger = logger or get_log()
        self._lines = []
        atexit.register(self.close)

    def evaluate(self, cmds, logger=None, out_file='', log=True,
                 timeout=None):
        """Perform the low-level interaction with an Octave Session
        """
        self.logger = logger or self.logger
        engine = self.engine
        self._lines = []

        if not engine:
            raise Oct2PyError('Session Closed, try a restart()')

        if logger and log:
            engine.stream_handler = self._log_line
        else:
            engine.stream_handler = self._lines.append

        engine.eval('clear("ans", "_", "a__");', timeout=timeout)

        for cmd in cmds:
            if cmd:
                try:
                    engine.eval(cmd, timeout=timeout)
                except EOF:
                    self.close()
                    raise Oct2PyError('Session is closed')
        resp = '\n'.join(self._lines).rstrip()

        if 'parse error:' in resp:
            raise Oct2PyError('Syntax Error:\n%s' % resp)

        if 'error:' in resp:
            if len(cmds) == 5:
                main_line = cmds[2].strip()
            else:
                main_line = '\n'.join(cmds)
            msg = ('Oct2Py tried to run:\n"""\n{0}\n"""\n'
                   'Octave returned:\n{1}'
                   .format(cmds[0], resp))
            raise Oct2PyError(msg)

        if out_file:
            save_ans = """
            if exist("ans") == 1,
                _ = ans;
            end,
            if exist("ans") == 1,
                if exist("a__") == 0,
                    save -v6 -mat-binary %(out_file)s _;
                end,
            end;""" % locals()
            engine.eval(save_ans.strip().replace('\n', ''),
                        timeout=timeout)

        return resp

    def handle_plot_settings(self, plot_dir=None, plot_name='plot',
            plot_format='svg', plot_width=None, plot_height=None,
            plot_res=None):
        if not self.engine:
            return
        settings = dict(backend='inline' if plot_dir else 'gnuplot',
                        format=plot_format,
                        name=plot_name,
                        width=plot_width,
                        height=plot_height,
                        resolution=plot_res)
        self.engine.plot_settings = settings

    def extract_figures(self, plot_dir):
        if not self.engine:
            return
        return self.engine.extract_figures(plot_dir)

    def make_figures(self, plot_dir=None):
        if not self.engine:
            return
        return self.engine.make_figures(plot_dir)

    def interrupt(self):
        if not self.engine:
            return
        self.proc.kill(signal.SIGINT)

    def close(self):
        """Cleanly close an Octave session
        """
        if not self.engine:
            return
        proc = self.proc
        try:
            proc.sendline('\nexit')
        except Exception as e:  # pragma: no cover
            self.logger.debug(e)

        try:
            proc.kill(signal.SIGTERM)
            time.sleep(0.1)
            proc.kill(signal.SIGKILL)
        except Exception as e:  # pragma: no cover
            self.logger.debug(e)

        self.proc = None
        self.engine = None

    def _log_line(self, line):
        self._lines.append(line)
        self.logger.debug(line)

    def _handle_stdin(self, line):
        """Handle a stdin request from the session."""
        return input(line.replace(STDIN_PROMPT, ''))

    def __del__(self):
        try:
            self.close()
        except:
            pass
