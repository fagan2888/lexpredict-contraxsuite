#!/bin/bash

# Switch apt sources from http to https

# This script is assumed to be run with sudo.
# This script is used for embedding into the worker init script.
# Please keep it simple and don't source other scripts from this one.

pushd /etc/apt

mv sources.list sources.list.backup

cat > sources.list <<EOF
## Note, this file is written by cloud-init on first boot of an instance
## modifications made here will not survive a re-bundle.
## if you wish to make changes you can:
## a.) add 'apt_preserve_sources_list: true' to /etc/cloud/cloud.cfg
##     or do the same in user-data
## b.) add sources in /etc/apt/sources.list.d
## c.) make changes to template file /etc/cloud/templates/sources.list.tmpl
# See http://help.ubuntu.com/community/UpgradeNotes for how to upgrade to
# newer versions of the distribution.
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic main restricted
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic main restricted
## Major bug fix updates produced after the final release of the
## distribution.
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-updates main restricted
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-updates main restricted
## N.B. software from this repository is ENTIRELY UNSUPPORTED by the Ubuntu
## team. Also, please note that software in universe WILL NOT receive any
## review or updates from the Ubuntu security team.
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic universe
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic universe
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-updates universe
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-updates universe
## N.B. software from this repository is ENTIRELY UNSUPPORTED by the Ubuntu
## team, and may not be under a free licence. Please satisfy yourself as to
## your rights to use the software. Also, please note that software in
## multiverse WILL NOT receive any review or updates from the Ubuntu
## security team.
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic multiverse
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic multiverse
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-updates multiverse
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-updates multiverse
## N.B. software from this repository may not have been tested as
## extensively as that contained in the main release, although it includes
## newer versions of some applications which may provide useful features.
## Also, please note that software in backports WILL NOT receive any review
## or updates from the Ubuntu security team.
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-backports main restricted universe multiverse
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-backports main restricted universe multiverse
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-security main restricted
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-security main restricted
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-security universe
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-security universe
deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-security multiverse
deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ bionic-security multiverse
## Uncomment the following two lines to add software from Canonical's
## 'partner' repository.
## This software is not part of Ubuntu, but is offered by Canonical and the
## respective vendors as a service to Ubuntu users.
# deb https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ubuntu xenial partner
# deb-src https://www.mirrorservice.org/sites/archive.ubuntu.com/ubuntu/ xenial partner
# deb-src http://apt.postgresql.org/pub/repos/apt/ xenial-pgdg main
# deb https://apt.postgresql.org/pub/repos/apt/ xenial-pgdg main
# deb-src https://apt.postgresql.org/pub/repos/apt/ xenial-pgdg main
# deb-src [arch=amd64] https://download.docker.com/linux/ubuntu bionic stable
# deb [arch=amd64] https://download.docker.com/linux/ubuntu bionic stable
EOF
chmod 644 sources.list
popd
