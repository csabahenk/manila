#!/bin/bash -xe
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# This script is executed inside pre_test_hook function in devstack gate.

# Install manila devstack integration
cp -r $BASE/new/manila/contrib/devstack/* $BASE/new/devstack

localrc_path=$BASE/new/devstack/localrc
echo "DEVSTACK_GATE_TEMPEST_ALLOW_TENANT_ISOLATION=1" >> $localrc_path
echo "API_RATE_LIMIT=False" >> $localrc_path
echo "TEMPEST_SERVICES+=,manila" >> $localrc_path
echo "VOLUME_BACKING_FILE_SIZE=22G" >> $localrc_path

echo "MANILA_BACKEND1_CONFIG_GROUP_NAME=london" >> $localrc_path
echo "MANILA_BACKEND2_CONFIG_GROUP_NAME=paris" >> $localrc_path
echo "MANILA_SHARE_BACKEND1_NAME=LONDON" >> $localrc_path
echo "MANILA_SHARE_BACKEND2_NAME=PARIS" >> $localrc_path

# JOB_NAME is defined in openstack-infra/config project
# used by CI/CD, where this script is intended to be used.
if [[ "$JOB_NAME" =~ "multibackend" ]]; then
    echo "MANILA_MULTI_BACKEND=True" >> $localrc_path
else
    echo "MANILA_MULTI_BACKEND=False" >> $localrc_path
fi

# Install manila tempest integration
cp -r $BASE/new/manila/contrib/tempest/tempest/* $BASE/new/tempest/tempest
