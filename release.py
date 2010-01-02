"""%prog RELEASE_AREA [action ...]

Perform needed actions to release package, doing the work in directory
RELEASE_AREA.

If no actions are given, print the tree of actions and do nothing.

Note that some ("clean*") actions do rm -rf on RELEASE_AREA or subdirectories
of RELEASE_AREA.  Other actions install software.
"""

# This script depends on the code from this git repository:
# git://github.com/jjlee/mechanize-build-tools.git

import cStringIO as StringIO
import datetime
import optparse
import os
import rfc822
import sys

import action_tree
import cmd_env

import buildtools.release as release
import buildtools.testdeb


class Test(object):

    def set_up(self, env):
        env.cmd(cmd_env.write_file_cmd(
                "imported.py",
                """\
print "spam"
if False:
    print "eggs"
"""))
        env.cmd(cmd_env.write_file_cmd(
                "test.py",
                "import imported\n"))
        env.cmd(cmd_env.write_file_cmd(
                "run_test.sh",
                """\
figleaf test.py
figleaf2html
cat html/_*_imported.py.html
"""))

    def run_test(self, env):
        env.cmd(["sh", "run_test.sh"])

    def verify(self, output):
        return output == """\
spam
figleaf: HTML output written to html
source file: <b>/tmp/figleaf-test/bind-mount/test/imported.py</b><br>


file stats: <b>3 lines, 2 executed: 66.7% covered</b>
<pre>
<font color="green">   1. print &quot;spam&quot;</font>
<font color="green">   2. if False:</font>
<font color="red">   3.     print &quot;eggs&quot;</font>
</pre>

"""


# Taken from git-buildpackage.  Should really use --git-posttag to record
# actual tag name rather than assuming this won't change.
def sanitize_version(version):
    if ':' in version: # strip of any epochs
        version = version.split(':', 1)[1]
    return version.replace('~', '.')


def split_debian_version(debian_version):
    return debian_version.rpartition("-")


def split_upstream_version(upstream_version):
    rest, date, build_num = upstream_version.rsplit("-", 2)
    assert len(date) == 8, date
    assert rest.endswith(".dev")
    return [rest, "-", date, "-", build_num]


def next_debian_version(version, upstream_version=None):
    upstream, sep, debian_version = split_debian_version(version)
    deb_rest, ppa_sep, ppa_num = debian_version.rpartition("~ppa")
    if upstream_version is None:
        # we're building a new .orig.tar.gz, reset ppa build number and
        # increment .orig.tar.gz build number if required
        rest, sep2, date, sep3, build_num = split_upstream_version(upstream)
        todays_date = datetime.date.today().strftime("%Y%m%d")
        if date == todays_date:
            next_build_num = int(build_num) + 1
        else:
            next_build_num = 0
        next_ppa_num = 1
        upstream_version = "".join(
            [rest, sep2, todays_date, sep3, str(next_build_num)])
    else:
        # we're using an existing .orig.tar.gz, increment ppa build number
        next_ppa_num = int(ppa_num) + 1
    debian_version = "".join([deb_rest, ppa_sep, str(next_ppa_num)])
    return "".join([upstream_version, sep, debian_version])


class Releaser(object):

    def __init__(self, env, git_repository_path, release_dir,
                 branch, upstream_branch, upstream_version=None,
                 run_in_repository=False, ignore_new=False):
        self._env = release.GitPagerWrapper(env)
        self._source_repo_path = git_repository_path
        self._in_source_repo = release.CwdEnv(self._env,
                                              self._source_repo_path)
        self._clone_path = os.path.join(release_dir, "clone")
        self._in_clone = release.CwdEnv(self._env, self._clone_path)
        if run_in_repository:
            self._in_repo = self._in_source_repo
            self._repo_path = self._source_repo_path
        else:
            self._in_repo = self._in_clone
            self._repo_path = self._clone_path
        self._release_dir = release_dir
        self._in_release_dir = release.CwdEnv(self._env, self._release_dir)
        self._branch = branch
        self._upstream_branch = upstream_branch
        self._upstream_version = upstream_version
        self._ignore_new = ignore_new

    def _git_buildpackage_cmd(self):
        cmd = ["git-buildpackage",
                "--git-upstream-branch=%s" % self._upstream_branch,
                "--git-debian-branch=%s" % self._branch]
        if self._ignore_new:
            cmd.append("--git-ignore-new")
        return cmd

    def _git_buildpackage_build_cmd(self):
        return self._git_buildpackage_cmd() + [
            "--git-pristine-tar",
            "-k%s" % self._get_key(),  # for signing packages
            ]

    def _git_buildpackage_tag_cmd(self):
        return self._git_buildpackage_cmd() + [
            "--git-tag-only",
            "--git-sign-tags",
            "--git-keyid=%s" % self._get_key(),  # for signing tags
            ]

    def _get_version_from_changelog(self):
        output = release.get_cmd_stdout(
            self._in_repo, ["dpkg-parsechangelog", "--format", "rfc822"])
        message = rfc822.Message(StringIO.StringIO(output))
        [version] = message.getheaders("Version")
        return version

    def _get_from_git_config(self, name):
        value = release.get_cmd_stdout(self._in_repo,
                                       ["git", "config", "--get", name])
        return release.trim(value, "\n")

    def _get_key(self):
        return self._get_from_git_config("user.signingkey")

    def install_deps(self, log):
        def ensure_installed(package_name, ppa=None):
            release.ensure_installed(self._env,
                                     cmd_env.PrefixCmdEnv(["sudo"], self._env),
                                     package_name, ppa)
        ensure_installed("build-essential")
        # TODO: does git-buildpackage satisfy build deps?  If not, should
        # probably replace this line with ensure_installed("pbuilder"), and
        # make this script run /usr/lib/pbuilder/pbuilder-satisfydepends
        ensure_installed("git-buildpackage")

    def clean(self, log):
        self._env.cmd(release.rm_rf_cmd(self._release_dir))

    def clone(self, log):
        self._env.cmd(["git", "clone",
                       self._source_repo_path, self._clone_path])
        self._in_clone.cmd(["git", "checkout", self._branch])

    def _next_tag(self):
        return next_debian_version(self._get_version_from_changelog(),
                                   self._upstream_version)

    def print_next_tag(self, log):
        print self._next_tag()

    def update_changelog(self, log):
        version = self._next_tag()
        name = self._get_from_git_config("user.name")
        email = self._get_from_git_config("user.email")
        entry = "Automated release."
        self._in_repo.cmd([
                "env", "DEBFULLNAME=%s" % name, "DEBEMAIL=%s" % email,
                "dch", "--newversion", version, entry])

    def commit_changelog(self, log):
        self._in_repo.cmd(["git", "commit", "-m", "Update changelog",
                           "debian/changelog"])

    def tag(self, log):
        self._in_repo.cmd(self._git_buildpackage_tag_cmd())

    def pristine_tar(self, log):
        # TODO: for final release, write empty setup.cfg
        self._in_repo.cmd(["python", "setup.py", "sdist", "--formats=gztar"])
        [tarball] = os.listdir(os.path.join(self._repo_path, "dist"))
        version = self._get_version_from_changelog()
        # rename so that git-buildpackage --pristine-tar finds it
        # TODO: get source package name from dpkg-parsechangelog
        source_package_name = "python-figleaf"
        upstream_version, sep, unused = split_debian_version(version)
        orig = "%s_%s.orig.tar.gz" % (source_package_name, upstream_version)
        self._in_repo.cmd(["mv", os.path.join("dist", tarball), orig])
        self._in_repo.cmd(["pristine-tar", "commit", orig,
                           self._upstream_branch])
        self._in_repo.cmd(["rm", orig])

    def build_debian_package(self, log):
        self._in_repo.cmd(self._git_buildpackage_build_cmd())

    def build_debian_source_package(self, log):
        self._in_repo.cmd(self._git_buildpackage_build_cmd() + ["-S"])

    def submit_to_ppa(self, log):
        dput_cf = os.path.join(self._release_dir, "dput.cf")
        self._env.cmd(cmd_env.write_file_cmd(dput_cf, """\
[figleaf]
fqdn = ppa.launchpad.net
method = ftp
incoming = ~jjl/figleaf/ubuntu/
login = anonymous
allow_unsigned_uploads = 0
"""))
        version = self._get_version_from_changelog()
        changes = os.path.join(
                self._repo_path, "..",
                "python-figleaf_%s_source.changes" % version)
        print "changes %r" % changes
        self._env.cmd(["dput", "--config", dput_cf, "figleaf", changes])

    def push(self, log):
        # TODO: get remote from source repo
        remote = "git@github.com:jjlee/figleaf.git"
        # changelog update
        self._in_repo.cmd(["git", "push", remote, self._branch])
        # pristine-tar data
        self._in_repo.cmd(["git", "push", remote, "pristine-tar"])
        # tag
        version = self._get_version_from_changelog()
        tag_name = "debian/%s" % sanitize_version(version)
        self._in_repo.cmd(["git", "push", remote, "tag", tag_name])

    def _get_deb_path(self):
        version = self._get_version_from_changelog()
        return os.path.join(
            self._repo_path, "..",
            "python-figleaf_%s_all.deb" % version)

    @action_tree.action_node
    def all(self):
        # TODO: use a subdirectory of work dir, not hard-coded /tmp path (think
        # I was being paranoid about possible pbuilder chroot cleanup bugs)
        work_dir = "/tmp/figleaf-test"
        test = buildtools.testdeb.PbuilderActions(self._env, work_dir,
                                                  self._get_deb_path,
                                                  test=Test())
        return [
            self.install_deps,
            self.clean,
            self.clone,
            self.print_next_tag,
            self.update_changelog,
            self.commit_changelog,
            self.tag,
            self.pristine_tar,
            self.build_debian_package,
            self.build_debian_source_package,
            ("debian_test", test.all),
            self.submit_to_ppa,
            self.push,
            ]


def parse_options(args):
    parser = optparse.OptionParser(usage=__doc__.strip())
    release.add_basic_env_options(parser)
    parser.add_option("--git-repository", metavar="DIRECTORY",
                      help="path to mechanize git repository (default is cwd)")
    parser.add_option("--branch", metavar="BRANCH",
                      default="master",
                      help="build from git branch BRANCH")
    parser.add_option("--upstream-branch", metavar="BRANCH",
                      default=None,
                      help=("use git branch BRANCH as upstream branch "
                            "(default is to use same branch as --branch)"))
    parser.add_option("--upstream-version", metavar="VERSION",
                      help=("Update changelog for upstream version VERSION.  "
                            "This will cause the corresponding .orig.tar.gz "
                            "to be used to build the package.  VERSION is the "
                            "part of the full debian package version before "
                            "the final '-'"))
    parser.add_option("--in-source-repository", action="store_true",
                      dest="in_repository",
                      help=("run all commands in original repository "
                            "(specified by --git-repository), rather than in "
                            "the clone of it in the release area"))
    parser.add_option("--ignore-new", action="store_true",
                      help=("Allow building packages with uncommitted changes "
                            "in working tree"))
    options, remaining_args = parser.parse_args(args)
    nr_args = len(remaining_args)
    try:
        options.release_area = remaining_args.pop(0)
    except IndexError:
        parser.error("Expected at least 1 argument, got %d" % nr_args)
    if options.upstream_branch is None:
        options.upstream_branch = options.branch
    return options, remaining_args


def main(argv):
    options, action_tree_args = parse_options(argv[1:])
    env = release.get_env_from_options(options)
    git_repository_path = options.git_repository
    if git_repository_path is None:
        git_repository_path = os.getcwd()
    releaser = Releaser(env, git_repository_path, options.release_area,
                        options.branch, options.upstream_branch,
                        options.upstream_version,
                        options.in_repository, options.ignore_new)
    action_tree.action_main(releaser.all, action_tree_args)


if __name__ == "__main__":
    main(sys.argv)
