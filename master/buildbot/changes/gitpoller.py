import time
import tempfile
import os

from twisted.python import log
from twisted.internet import defer, utils

from buildbot.changes import base, changes

class GitPoller(base.PollingChangeSource):
    """This source will poll a remote git repo for changes and submit
    them to the change master."""
    
    compare_attrs = ["repourl", "branch", "workdir",
                     "pollInterval", "gitbin", "usetimestamps",
                     "category", "project"]
                     
    def __init__(self, repourl, branch='master', 
                 workdir=None, pollInterval=10*60, 
                 gitbin='git', usetimestamps=True,
                 category=None, project=None):
        self.repourl = repourl
        self.branch = branch
        self.pollInterval = pollInterval
        self.lastChange = time.time()
        self.lastPoll = time.time()
        self.gitbin = gitbin
        self.workdir = workdir
        self.usetimestamps = usetimestamps
        self.category = category
        self.project = project
        self.changeCount = 0
        self.commitInfo  = {}
        
        if self.workdir == None:
            self.workdir = tempfile.gettempdir() + '/gitpoller_work'

    def startService(self):
        base.PollingChangeSource.startService(self)
        
        if not os.path.exists(self.workdir):
            log.msg('gitpoller: creating working dir %s' % self.workdir)
            os.makedirs(self.workdir)
            
        if not os.path.exists(self.workdir + r'/.git'):
            log.msg('gitpoller: initializing working dir')
            os.system(self.gitbin + ' clone ' + self.repourl + ' ' + self.workdir)
        
    def describe(self):
        status = ""
        if not self.parent:
            status = "[STOPPED - check log]"
        str = 'GitPoller watching the remote git repository %s, branch: %s %s' \
                % (self.repourl, self.branch, status)
        return str

    def poll(self):
        d = self._get_changes()
        d.addCallback(self._process_changes)
        d.addErrback(self._changes_finished_failure)
        d.addCallback(self._catch_up)
        d.addCallback(self._catch_up_finished)
        d.addErrback(self._catch_up_finished_failure)
        return d

    def _get_commit_comments(self, rev):
        args = ['log', rev, '--no-walk', r'--format=%s%n%b']
        d = utils.getProcessOutput(self.gitbin, args, path=self.workdir, env={}, errortoo=False )
        d.addCallback(self._get_commit_comments_from_output)
        return d

    def _get_commit_comments_from_output(self,git_output):
        stripped_output = git_output.strip()
        log.msg('gitpoller: _get_commit_comments_from_output "%s" for "%s"' % (stripped_output, self.repourl))
        if len(stripped_output) == 0:
            raise EnvironmentError('could not get commit comment for rev')
        self.commitInfo['comments'] = stripped_output
        return self.commitInfo['comments'] # for tests

    def _get_commit_timestamp(self, rev):
        # unix timestamp
        args = ['log', rev, '--no-walk', r'--format=%ct']
        d = utils.getProcessOutput(self.gitbin, args, path=self.workdir, env={}, errortoo=False )
        d.addCallback(self._get_commit_timestamp_from_output)
        return d

    def _get_commit_timestamp_from_output(self, git_output):
        stripped_output = git_output.strip()
        if self.usetimestamps:
            try:
                stamp = float(stripped_output)
            except Exception, e:
                    log.msg('gitpoller: caught exception converting output \'%s\' to timestamp' % stripped_output)
                    raise e
            self.commitInfo['timestamp'] = stamp
        else:
            self.commitInfo['timestamp'] = None
        return self.commitInfo['timestamp'] # for tests

    def _get_commit_files(self, rev):
        args = ['log', rev, '--name-only', '--no-walk', r'--format=%n']
        d = utils.getProcessOutput(self.gitbin, args, path=self.workdir, env={}, errortoo=False )
        d.addCallback(self._get_commit_files_from_output)
        return d

    def _get_commit_files_from_output(self, git_output):
        fileList = git_output.split()
        self.commitInfo['files'] = fileList
        return self.commitInfo['files'] # for tests
            
    def _get_commit_name(self, rev):
        args = ['log', rev, '--no-walk', r'--format=%aE']
        d = utils.getProcessOutput(self.gitbin, args, path=self.workdir, env={}, errortoo=False )
        d.addCallback(self._get_commit_name_from_output)
        return d

    def _get_commit_name_from_output(self, git_output):
        stripped_output = git_output.strip()
        log.msg('gitpoller: _get_commit_name_from_output "%s" for "%s"' % (stripped_output, self.repourl))
        if len(stripped_output) == 0:
            raise EnvironmentError('could not get commit name for rev')
        self.commitInfo['name'] = stripped_output
        return self.commitInfo['name'] # for tests

    def _get_changes(self):
        log.msg('gitpoller: polling git repo at %s' % self.repourl)

        self.lastPoll = time.time()
        
        # get a deferred object that performs the fetch
        args = ['fetch', self.repourl, self.branch]
        # This command always produces data on stderr, but we actually do not care
        # about the stderr or stdout from this command. We set errortoo=True to
        # avoid an errback from the deferred. The callback which will be added to this
        # deferred will not use the response.
        d = utils.getProcessOutput(self.gitbin, args, path=self.workdir, env={}, errortoo=True )

        return d

    def _process_changes(self, unused_output):
        #log.msg('gitpoller: _process_changes called with ARG "%s"' % res)
        # get the change list
        revListArgs = ['log', 'HEAD..FETCH_HEAD', r'--format=%H']
        d = utils.getProcessOutput(self.gitbin, revListArgs, path=self.workdir, env={}, errortoo=False )
        d.addCallback(self._process_changes_in_output)
        return d
    
    def _process_changes_in_output(self, git_output):
        log.msg('gitpoller: _process_changes_in_output with "%s"' % git_output)
        self.changeCount = 0
        
        # process oldest change first
        revList = git_output.split()
        if revList:
            revList.reverse()
            self.changeCount = len(revList)
            
        log.msg('gitpoller: processing %d changes: "%s" in "%s"' % (self.changeCount, revList, self.workdir) )

        for rev in revList:
            log.msg('gitpoller: _process_changes_in_output "%s" in "%s"' % (rev, self.workdir))
            self.commitInfo = {}

            deferreds = [
                                self._get_commit_timestamp(rev),
                                self._get_commit_name(rev),
                                self._get_commit_files(rev),
                                self._get_commit_comments(rev),
                        ]
            log.msg('gitpoller: _process_changes_in_output deferreds "%s" in "%s"' % (deferreds, self.workdir))
            dl = defer.DeferredList(deferreds)
            dl.addCallback(self._add_change,rev)        


    def _add_change(self, results, rev):
        log.msg('gitpoller: _add_change results: "%s", rev: "%s" in "%s"' % (results, rev, self.workdir))

        c = changes.Change(who=self.commitInfo['name'],
                               revision=rev,
                               files=self.commitInfo['files'],
                               comments=self.commitInfo['comments'],
                               when=self.commitInfo['timestamp'],
                               branch=self.branch,
                               category=self.category,
                               project=self.project,
                               repository=self.repourl)
        log.msg('gitpoller: change "%s" in "%s"' % (c, self.workdir))
        self.parent.addChange(c)
        self.lastChange = self.lastPoll
            

    def _changes_finished_failure(self, f):
        log.msg('gitpoller: repo poll failed')
        log.err(f)
        # eat the failure to continue along the defered chain 
        # - we still want to catch up
        return None
        
    def _catch_up(self, res):
        if self.changeCount == 0:
            log.msg('gitpoller: no changes, no catch_up')
            return self.changeCount
        log.msg('gitpoller: catching up to FETCH_HEAD')
        args = ['reset', '--hard', 'FETCH_HEAD']
        d = utils.getProcessOutputAndValue(self.gitbin, args, path=self.workdir, env={})
        return d;

    def _catch_up_finished(self, res):
        (stdout, stderr, code) = res
        if code != 0:
            raise EnvironmentError('catch up failed with exit code: %d' % code)

    def _catch_up_finished_failure(self, f):
        log.err(f)
        if self.parent:
            log.msg('gitpoller: stopping service - please resolve issues in local repo: %s' %
                self.workdir)
            self.stopService()
