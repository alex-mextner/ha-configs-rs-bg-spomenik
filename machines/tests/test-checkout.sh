#!/usr/bin/env bash
# dotfiles/debian/checkout (the git half of both dotfiles modules) against local repositories: a fresh clone at the
# pin, fast-forward, rollback, local commits, the refusals, the applied-pin ref across a force-push, submodules.
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

CHECKOUT=$MACHINES/dotfiles/debian/checkout
export HOME=$WORK/home
export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
# the submodule URLs here are local paths, which git refuses by default
export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=protocol.file.allow GIT_CONFIG_VALUE_0=always \
	GIT_CONFIG_KEY_1=init.defaultBranch GIT_CONFIG_VALUE_1=master \
	GIT_CONFIG_KEY_2=advice.detachedHead GIT_CONFIG_VALUE_2=false
mkdir -p "$HOME"

UP=$WORK/up.git DEV=$WORK/dev FILES=$HOME/.files
git init -q --bare "$UP"
git clone -q "$UP" "$DEV" 2>/dev/null

# commit REPO NAME: commit a new file NAME in REPO; print the sha
commit() {
	echo "$2" >"$1/$2"
	git -C "$1" add "$2"
	git -C "$1" commit -qm "$2"
	git -C "$1" rev-parse HEAD
}
co() { run bash "$CHECKOUT" "$1" "$FILES" "$UP" master "$2"; }
head_is() { [[ $(git -C "$FILES" rev-parse HEAD) == "$1" ]]; }
applied_is() { [[ $(git -C "$FILES" rev-parse -q --verify refs/dotfiles/applied) == "$1" ]]; }
converged() { [[ $code == 0 && -z $out ]]; }

c1=$(commit "$DEV" c1)
c2=$(commit "$DEV" c2)
git -C "$DEV" push -q origin master

co status master
expect "a rev that is not a full sha is refused (exit 2)" test "$code" = 2

co status "$c1"
expect "status: a missing checkout is a clone" contains "$out" "clone $UP"
expect "status: changes nothing" test ! -e "$FILES"
co apply "$c1"
expect "clone lands on the pin, not on master" head_is "$c1"
expect "clone is on master" test "$(git -C "$FILES" symbolic-ref --short HEAD)" = master
expect "clone records the pin" applied_is "$c1"
expect "no clone left next to the checkout" test ! -e "$FILES.clone"
co status "$c1"
expect "converged after the clone" converged

co apply "$c2"
expect "a newer pin fast-forwards" head_is "$c2"
expect "the newer pin is recorded" applied_is "$c2"
co apply "$c1"
expect "a rolled-back pin moves back" head_is "$c1"
expect "the rolled-back pin is recorded" applied_is "$c1"

l1=$(commit "$FILES" local1)
co status "$c1"
expect "a local commit on top of the pin is converged" converged
co apply "$c1"
expect "the local commit stays" head_is "$l1"

# the new pin is not fetched yet: the first run already names the local work (not git's fast-forward error)
c3=$(commit "$DEV" c3)
git -C "$DEV" push -q origin master
co status "$c3"
expect "status, offline: an unknown pin needs a fetch" contains "$out" "fetch master and fast-forward"
co apply "$c3"
expect "local work on an older pin is refused on the first run (exit 1)" test "$code" = 1
expect "  ... naming it" contains "$err" "local work on an older pin"
expect "  ... the checkout is untouched" head_is "$l1"

git -C "$FILES" reset -q --hard "$c3"
l2=$(commit "$FILES" local2)
co apply "$c1"
expect "upstream commits past the pin under a local one are refused (exit 1)" test "$code" = 1
expect "  ... naming them" contains "$err" "mixed with 1 local one"
expect "  ... the checkout is untouched" head_is "$l2"

git -C "$FILES" reset -q --hard "$c3"
co apply "$c3"
expect "back at the pin c3" head_is "$c3"
expect "c3 recorded" applied_is "$c3"

# master is force-pushed: c2 and c3 leave origin/master, and a host has fetched that
git -C "$DEV" reset -q --hard "$c1"
d1=$(commit "$DEV" d1)
git -C "$DEV" push -q --force origin master
git -C "$FILES" fetch -q origin
co apply "$d1"
expect "a pin on the rewritten master replaces the earlier pin's commits" head_is "$d1"
co apply "$c3"
expect "and the pin can go back to c3, which is on no branch upstream" head_is "$c3"
cp -a "$FILES" "$WORK/without-ref"
git -C "$WORK/without-ref" update-ref -d refs/dotfiles/applied
run bash "$CHECKOUT" status "$WORK/without-ref" "$UP" master "$c1"
expect "(without refs/dotfiles/applied, c2 and c3 would be local work and stay)" lacks "$out" "move"
co status "$c1"
expect "with it, rolling back to c1 drops c2 and c3" contains "$out" "move $FILES to the pinned"
co apply "$c1"
expect "  ... and moves back" head_is "$c1"

co apply 0123456789abcdef0123456789abcdef01234567
expect "a sha the repository does not have fails (exit 1)" test "$code" = 1
expect "  ... after a fetch" contains "$err" "not even after fetching"
expect "  ... leaving the checkout" head_is "$c1"

git -C "$FILES" checkout -q -b wip
co apply "$d1"
expect "another checked-out branch is left alone (exit 0)" test "$code" = 0
expect "  ... saying so" contains "$err" "is not on master"
expect "  ... unchanged" head_is "$c1"
git -C "$FILES" checkout -q master

mkdir -p "$WORK/plain"
run bash "$CHECKOUT" status "$WORK/plain" "$UP" master "$c1"
expect "a directory that is no checkout is refused (exit 1)" test "$code" = 1

# Submodules: the repository of `sub` moves (its old URL disappears) and the pin moves to a commit only the new one
# has. The superproject's fetch must not recurse (the old URL fails), and the submodule's URL is synced first.
SUB1=$WORK/sub1.git SUB2=$WORK/sub2.git
git init -q --bare "$SUB1"
git clone -q "$SUB1" "$WORK/subdev" 2>/dev/null
s1=$(commit "$WORK/subdev" s1)
git -C "$WORK/subdev" push -q origin master
git -C "$DEV" submodule add -q "$SUB1" sub
git -C "$DEV" commit -qm "add sub"
p1=$(git -C "$DEV" rev-parse HEAD)
git -C "$DEV" push -q origin master
co apply "$p1"
git -C "$FILES" submodule update -q --init # what install-ubuntu.sh does
expect "submodule at its recorded commit" test "$(git -C "$FILES/sub" rev-parse HEAD)" = "$s1"

git clone -q --bare "$SUB1" "$SUB2"
git -C "$WORK/subdev" remote set-url origin "$SUB2"
s2=$(commit "$WORK/subdev" s2)
git -C "$WORK/subdev" push -q origin master
rm -rf "$SUB1"
git -C "$DEV/sub" remote set-url origin "$SUB2"
git -C "$DEV/sub" fetch -q origin
git -C "$DEV/sub" checkout -q "$s2"
git -C "$DEV" config -f .gitmodules submodule.sub.url "$SUB2"
git -C "$DEV" commit -qam "sub moved"
p2=$(git -C "$DEV" rev-parse HEAD)
git -C "$DEV" push -q origin master
co status "$p2"
expect "status: an unknown pin with submodules still only needs a fetch" contains "$out" "fetch master"
co apply "$p2"
expect "a moved submodule: apply succeeds" test "$code" = 0
expect "  ... superproject at the pin" head_is "$p2"
expect "  ... submodule at its new commit" test "$(git -C "$FILES/sub" rev-parse HEAD)" = "$s2"
expect "  ... from its new URL" test "$(git -C "$FILES/sub" remote get-url origin)" = "$SUB2"
co status "$p2"
expect "  ... then converged" converged

git -C "$FILES/sub" checkout -q -b wip
s3=$(commit "$WORK/subdev" s3)
git -C "$WORK/subdev" push -q origin master
git -C "$DEV/sub" fetch -q origin
git -C "$DEV/sub" checkout -q "$s3"
git -C "$DEV" commit -qam "sub s3"
p3=$(git -C "$DEV" rev-parse HEAD)
git -C "$DEV" push -q origin master
co apply "$p3"
expect "a submodule on a branch keeps its commit" test "$(git -C "$FILES/sub" rev-parse HEAD)" = "$s2"
expect "  ... and its branch" test "$(git -C "$FILES/sub" symbolic-ref --short HEAD)" = wip

# a pin given as an annotated tag's sha: HEAD and the applied ref hold the commit it names
git -C "$DEV" tag -a -m pinned pinned "$p2"
git -C "$DEV" push -q origin pinned
tag=$(git -C "$DEV" rev-parse pinned)
git -C "$FILES" fetch -q origin tag pinned
co apply "$tag"
expect "an annotated tag's sha as the pin: moves to its commit" head_is "$p2"
expect "  ... records the commit" applied_is "$p2"
co status "$tag"
expect "  ... then converged" converged

done_testing
