# Copyright 2017, David Wilson
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from __future__ import absolute_import
import logging
import os
import shlex
import sys
import time

import ansible.errors
import ansible.plugins.connection

import mitogen.unix
import mitogen.utils

import ansible_mitogen.target
import ansible_mitogen.process
import ansible_mitogen.services


LOG = logging.getLogger(__name__)


class Connection(ansible.plugins.connection.ConnectionBase):
    #: mitogen.master.Broker for this worker.
    broker = None

    #: mitogen.master.Router for this worker.
    router = None

    #: mitogen.master.Context representing the parent Context, which is
    #: presently always the master process.
    parent = None

    #: mitogen.master.Context connected to the target machine's initial SSH
    #: account.
    host = None

    #: mitogen.master.Context connected to the target user account on the
    #: target machine (i.e. via sudo), or simply a copy of :attr:`host` if
    #: become is not in use.
    context = None

    #: Only sudo is supported for now.
    become_methods = ['sudo']

    #: Set by the constructor according to whichever connection type this
    #: connection should emulate. We emulate the original connection type to
    #: work around artificial limitations in e.g. the synchronize action, which
    #: hard-codes 'local' and 'ssh' as the only allowable connection types.
    transport = None

    #: Set to 'ansible_python_interpreter' by on_action_run().
    python_path = None

    #: Set to 'ansible_sudo_exe' by on_action_run().
    sudo_path = None

    #: Set to 'ansible_ssh_timeout' by on_action_run().
    ansible_ssh_timeout = None

    #: Set after connection to the target context's home directory.
    _homedir = None

    def __init__(self, play_context, new_stdin, original_transport, **kwargs):
        assert ansible_mitogen.process.MuxProcess.unix_listener_path, (
            'The "mitogen" connection plug-in may only be instantiated '
             'by the "mitogen" strategy plug-in.'
        )

        self.original_transport = original_transport
        self.transport = original_transport
        self.kwargs = kwargs
        super(Connection, self).__init__(play_context, new_stdin)

    def __del__(self):
        """
        Ansible cannot be trusted to always call close() e.g. the synchronize
        action constructs a local connection like this. So provide a destructor
        in the hopes of catching these cases.
        """
        # https://github.com/dw/mitogen/issues/140
        self.close()

    def on_action_run(self, task_vars):
        """
        Invoked by ActionModuleMixin to indicate a new task is about to start
        executing. We use the opportunity to grab relevant bits from the
        task-specific data.
        """
        self.ansible_ssh_timeout = task_vars.get(
            'ansible_ssh_timeout',
            None
        )
        self.python_path = task_vars.get(
            'ansible_python_interpreter',
            '/usr/bin/python'
        )
        self.sudo_path = task_vars.get(
            'ansible_sudo_exe',
            'sudo'
        )

        self.close(new_task=True)

    @property
    def homedir(self):
        self._connect()
        return self._homedir

    @property
    def connected(self):
        return self.context is not None

    def _on_connection_error(self, msg):
        raise ansible.errors.AnsibleConnectionFailure(msg)

    def _on_become_error(self, msg):
        # TODO: vanilla become failures yield this:
        #   {
        #       "changed": false,
        #       "module_stderr": "sudo: sorry, you must have a tty to run sudo\n",
        #       "module_stdout": "",
        #       "msg": "MODULE FAILURE",
        #       "rc": 1
        #   }
        #
        # Currently we yield this:
        #   {
        #       "msg": "EOF on stream; last 300 bytes received: 'sudo: ....\n'"
        #   }
        raise ansible.errors.AnsibleModuleError(msg)

    def _wrap_connect(self, on_error, kwargs):
        dct = mitogen.service.call(
            context=self.parent,
            handle=ansible_mitogen.services.ContextService.handle,
            method='get',
            kwargs=mitogen.utils.cast(kwargs),
        )

        if dct['msg']:
            on_error(dct['msg'])

        return dct['context'], dct['home_dir']

    def _connect_local(self):
        """
        Fetch a reference to the local() Context from ContextService in the
        master process.
        """
        return self._wrap_connect(self._on_connection_error, {
            'method_name': 'local',
            'python_path': self.python_path,
        })

    def _connect_ssh(self):
        """
        Fetch a reference to an SSH Context matching the play context from
        ContextService in the master process.
        """
        return self._wrap_connect(self._on_connection_error, {
            'method_name': 'ssh',
            'check_host_keys': False,  # TODO
            'hostname': self._play_context.remote_addr,
            'username': self._play_context.remote_user,
            'password': self._play_context.password,
            'port': self._play_context.port,
            'python_path': self.python_path,
            'identity_file': self._play_context.private_key_file,
            'ssh_path': self._play_context.ssh_executable,
            'connect_timeout': self.ansible_ssh_timeout,
            'ssh_args': [
                term
                for s in (
                    getattr(self._play_context, 'ssh_args', ''),
                    getattr(self._play_context, 'ssh_common_args', ''),
                    getattr(self._play_context, 'ssh_extra_args', '')
                )
                for term in shlex.split(s or '')
            ]
        })

    def _connect_docker(self):
        return self._wrap_connect(self._on_connection_error, {
            'method_name': 'docker',
            'container': self._play_context.remote_addr,
            'python_path': self.python_path,
            'connect_timeout': self._play_context.timeout,
        })

    def _connect_sudo(self, via=None, python_path=None):
        """
        Fetch a reference to a sudo Context matching the play context from
        ContextService in the master process.

        :param via:
            Parent Context of the sudo Context. For Ansible, this should always
            be a Context returned by _connect_ssh().
        """
        return self._wrap_connect(self._on_become_error, {
            'method_name': 'sudo',
            'username': self._play_context.become_user,
            'password': self._play_context.become_pass,
            'python_path': python_path or self.python_path,
            'sudo_path': self.sudo_path,
            'connect_timeout': self._play_context.timeout,
            'via': via,
            'sudo_args': [
                term
                for s in (
                    self._play_context.sudo_flags,
                    self._play_context.become_flags
                )
                for term in shlex.split(s or '')
            ],
        })

    def _connect(self):
        """
        Establish a connection to the master process's UNIX listener socket,
        constructing a mitogen.master.Router to communicate with the master,
        and a mitogen.master.Context to represent it.

        Depending on the original transport we should emulate, trigger one of
        the _connect_*() service calls defined above to cause the master
        process to establish the real connection on our behalf, or return a
        reference to the existing one.
        """
        if self.connected:
            return

        if not self.broker:
            self.broker = mitogen.master.Broker()
            self.router, self.parent = mitogen.unix.connect(
                path=ansible_mitogen.process.MuxProcess.unix_listener_path,
                broker=self.broker,
            )

        if self.original_transport == 'local':
            if self._play_context.become:
                self.context, self._homedir = self._connect_sudo(
                    python_path=sys.executable
                )
            else:
                self.context, self._homedir = self._connect_local()
            return

        if self.original_transport == 'docker':
            self.host, self._homedir = self._connect_docker()
        elif self.original_transport == 'ssh':
            self.host, self._homedir = self._connect_ssh()

        if self._play_context.become:
            self.context, self._homedir = self._connect_sudo(via=self.host)
        else:
            self.context = self.host

    def get_context_name(self):
        """
        Return the name of the target context we issue commands against, i.e. a
        unique string useful as a key for related data, such as a list of
        modules uploaded to the target.
        """
        return self.context.name

    def close(self, new_task=False):
        """
        Arrange for the mitogen.master.Router running in the worker to
        gracefully shut down, and wait for shutdown to complete. Safe to call
        multiple times.
        """
        for context in set([self.host, self.context]):
            if context:
                mitogen.service.call(
                    context=self.parent,
                    handle=ansible_mitogen.services.ContextService.handle,
                    method='put',
                    kwargs={
                        'context': context
                    }
                )

        self.host = None
        self.context = None
        if self.broker and not new_task:
            self.broker.shutdown()
            self.broker.join()
            self.broker = None
            self.router = None

    def call_async(self, func, *args, **kwargs):
        """
        Start a function call to the target.

        :returns:
            mitogen.core.Receiver that receives the function call result.
        """
        self._connect()
        return self.context.call_async(func, *args, **kwargs)

    def call(self, func, *args, **kwargs):
        """
        Start and wait for completion of a function call in the target.

        :raises mitogen.core.CallError:
            The function call failed.
        :returns:
            Function return value.
        """
        t0 = time.time()
        try:
            return self.call_async(func, *args, **kwargs).get().unpickle()
        finally:
            LOG.debug('Call %s%r took %d ms', func.func_name, args,
                      1000 * (time.time() - t0))

    def exec_command(self, cmd, in_data='', sudoable=True, mitogen_chdir=None):
        """
        Implement exec_command() by calling the corresponding
        ansible_mitogen.target function in the target.

        :param str cmd:
            Shell command to execute.
        :param bytes in_data:
            Data to supply on ``stdin`` of the process.
        :returns:
            (return code, stdout bytes, stderr bytes)
        """
        emulate_tty = (not in_data and sudoable)
        rc, stdout, stderr = self.call(
            ansible_mitogen.target.exec_command,
            cmd=mitogen.utils.cast(cmd),
            in_data=mitogen.utils.cast(in_data),
            chdir=mitogen_chdir,
            emulate_tty=emulate_tty,
        )

        stderr += 'Shared connection to %s closed.%s' % (
            self._play_context.remote_addr,
            ('\r\n' if emulate_tty else '\n'),
        )
        return rc, stdout, stderr

    def fetch_file(self, in_path, out_path):
        """
        Implement fetch_file() by calling the corresponding
        ansible_mitogen.target function in the target.

        :param str in_path:
            Remote filesystem path to read.
        :param str out_path:
            Local filesystem path to write.
        """
        output = self.call(ansible_mitogen.target.read_path,
                           mitogen.utils.cast(in_path))
        ansible_mitogen.target.write_path(out_path, output)

    def put_data(self, out_path, data):
        """
        Implement put_file() by caling the corresponding
        ansible_mitogen.target function in the target.

        :param str out_path:
            Remote filesystem path to write.
        :param byte data:
            File contents to put.
        """
        self.call(ansible_mitogen.target.write_path,
                  mitogen.utils.cast(out_path),
                  mitogen.utils.cast(data))

    def put_file(self, in_path, out_path):
        """
        Implement put_file() by streamily transferring the file via
        FileService.

        :param str in_path:
            Local filesystem path to read.
        :param str out_path:
            Remote filesystem path to write.
        """
        mitogen.service.call(
            context=self.parent,
            handle=ansible_mitogen.services.FileService.handle,
            method='register',
            kwargs={
                'path': mitogen.utils.cast(in_path)
            }
        )
        self.call(
            ansible_mitogen.target.transfer_file,
            context=self.parent,
            in_path=in_path,
            out_path=out_path
        )
