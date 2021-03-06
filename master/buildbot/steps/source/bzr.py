# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from twisted.python import log, failure
from twisted.internet import defer

from buildbot.process import buildstep
from buildbot.steps.source import Source
from buildbot.interfaces import BuildSlaveTooOldError

class Bzr(Source):

    name = 'bzr'
    renderables = [ 'repourl', 'baseURL' ]

    def __init__(self, repourl=None, baseURL=None, mode='incremental',
                 method=None, defaultBranch=None, **kwargs):
        
        self.repourl = repourl
        self.baseURL = baseURL
        self.branch = defaultBranch
        self.mode = mode
        self.method = method
        Source.__init__(self, **kwargs)
        self.addFactoryArguments(repourl=repourl,
                                 mode=mode,
                                 method=method,
                                 baseURL=baseURL,
                                 defaultBranch=defaultBranch,
                                 )
        if repourl and baseURL:
            raise ValueError("you must provide exactly one of repourl and"
                             " baseURL")

        if repourl is None and baseURL is None:
            raise ValueError("you must privide at least one of repourl and"
                             " baseURL")

        if self.repourl is None:
            self.repourl = self.baseURL + defaultBranch

        assert self.mode in ['incremental', 'full']

        if self.mode == 'full':
            assert self.method in ['clean', 'fresh', 'clobber', 'copy', None]

    def startVC(self, branch, revision, patch):
        self.branch = branch or 'master'
        self.revision = revision
        self.method = self._getMethod()
        self.stdio_log = self.addLog("stdio")

        d = self.checkBzr()
        def checkInstall(bzrInstalled):
            if not bzrInstalled:
                raise BuildSlaveTooOldError("bzr is not installed on slave")
            return 0

        d.addCallback(checkInstall)
        if self.mode == 'full':
            d.addCallback(lambda _: self.full())
        elif self.mode == 'incremental':
            d.addCallback(lambda _: self.incremental())

        d.addCallback(self.parseGotRevision)
        d.addCallback(self.finish)
        d.addErrback(self.failed)
        return d

    def incremental(self):
        d = self._sourcedirIsUpdatable()
        def _cmd(updatable):
            if updatable:
                command = ['update']
            else:
                command = ['checkout', self.repourl, '.']

            if self.revision:
                command.append(['-r', self.revision])
            return command

        d.addCallback(_cmd)
        d.addCallback(self._dovccmd)
        return d

    @defer.deferredGenerator
    def full(self):
        if self.method == 'clobber':
            wfd = defer.waitForDeferred(self.clobber())
            yield wfd
            wfd.getResult()
            return
        elif self.method == 'copy':
            self.workdir = 'source'
            wfd = defer.waitForDeferred(self.copy())
            yield wfd
            wfd.getResult()
            return

        wfd = defer.waitForDeferred(self._sourcedirIsUpdatable())
        yield wfd
        updatable = wfd.getResult()
        if not updatable:
            log.msg("No bzr repo present, making full checkout")
            d = self._doFull()
        elif self.method == 'clean':
            d = self.clean()
        elif self.method == 'fresh':
            d = self.fresh()
        else:
            raise ValueError("Unknown method, check your configuration")
        wfd = defer.waitForDeferred(d)
        yield wfd
        wfd.getResult()

    def clobber(self):
        cmd = buildstep.LoggedRemoteCommand('rmdir', {'dir': self.workdir,
                                                      'logEnviron': self.logEnviron,})
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def checkRemoval(res):
            if res != 0:
                raise RuntimeError("Failed to delete directory")
            return res
        d.addCallback(lambda _: checkRemoval(cmd.rc))
        d.addCallback(lambda _: self._doFull())
        return d

    def copy(self):
        cmd = buildstep.LoggedRemoteCommand('rmdir', {'dir': 'build',
                                                      'logEnviron': self.logEnviron,})
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        d.addCallback(lambda _: self.incremental())
        def copy(_):
            cmd = buildstep.LoggedRemoteCommand('cpdir',
                                                {'fromdir': 'source',
                                                 'todir':'build',
                                                 'logEnviron': self.logEnviron,})
            cmd.useLog(self.stdio_log, False)
            d = self.runCommand(cmd)
            return d
        d.addCallback(copy)
        return d

    def clean(self):
        d = self._dovccmd(['clean-tree', '--ignored', '--force'])
        command = ['update']
        if self.revision:
            command.append(['-r', self.revision])
        d.addCallback(lambda _: self._dovccmd(command))
        return d

    def fresh(self):
        d = self._dovccmd(['clean-tree', '--force'])
        command = ['update']
        if self.revision:
            command.append(['-r', self.revision])
        d.addCallback(lambda _: self._dovccmd(command))
        return d

    def _doFull(self):
        command = ['checkout', self.repourl, '.']
        if self.revision:
            command.append(['-r', self.revision])
        d = self._dovccmd(command)
        return d

    def finish(self, res):
        d = defer.succeed(res)
        def _gotResults(results):
            self.setStatus(self.cmd, results)
            log.msg("Closing log, sending result of the command %s " % \
                    (self.cmd))
            return results
        d.addCallback(_gotResults)
        d.addCallbacks(self.finished, self.checkDisconnect)
        return d

    def _sourcedirIsUpdatable(self):
        cmd = buildstep.LoggedRemoteCommand('stat', {'file': self.workdir + '/.bzr',
                                                     'logEnviron': self.logEnviron,})
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def _fail(tmp):
            if cmd.rc != 0:
                return False
            return True
        d.addCallback(_fail)
        return d

    def computeSourceRevision(self, changes):
        if not changes:
            return None
        lastChange = max([int(c.revision) for c in changes])
        return lastChange

    def _dovccmd(self, command, abandonOnFailure=True, collectStdout=False):
        cmd = buildstep.RemoteShellCommand(self.workdir, ['bzr'] + command,
                                           env=self.env,
                                           logEnviron=self.logEnviron,
                                           collectStdout=collectStdout)
        cmd.useLog(self.stdio_log, False)
        d = self.runCommand(cmd)
        def evaluateCommand(cmd):
            if abandonOnFailure and cmd.rc != 0:
                log.msg("Source step failed while running command %s" % cmd)
                raise failure.Failure(cmd.rc)
            if collectStdout:
                return cmd.stdout
            else:
                return cmd.rc
        d.addCallback(lambda _: evaluateCommand(cmd))
        return d

    def checkBzr(self):
        d = self._dovccmd(['--version'])
        def check(res):
            if res == 0:
                return True
            return False
        d.addCallback(check)
        return d

    def _getMethod(self):
        if self.method is not None and self.mode != 'incremental':
            return self.method
        elif self.mode == 'incremental':
            return None
        elif self.method is None and self.mode == 'full':
            return 'fresh'

    def parseGotRevision(self, _):
        d = self._dovccmd(["version-info", "--custom", "--template='{revno}"],
                          collectStdout=True)
        def setrev(stdout):
            revision = stdout.strip("'")
            try:
                revision = int(revision)
            except ValueError:
                log.msg("Invalid revision number")
                raise

            log.msg("Got Git revision %s" % (revision, ))
            self.setProperty('got_revision', revision, 'Source')
            return 0
        d.addCallback(setrev)
        return d
    
