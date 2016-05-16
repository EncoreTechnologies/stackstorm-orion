# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time

from st2actions.runners.pythonrunner import Action
from orionsdk import SwisClient

from lib.node import OrionNode
from lib.utils import send_user_error, is_ip


class OrionBaseAction(Action):
    def __init__(self, config):
        super(OrionBaseAction, self).__init__(config)

        self.client = None

        if "orion" not in self.config:
            raise ValueError("Orion host details not in the config.yaml")

    def connect(self, platform):
        """
        Connect to an Orion platform from the packs config.yaml.
        """
        if platform is None:
            try:
                platform = self.config['defaults']['platform']
            except IndexError:
                send_user_error("No default Orion platform.")
                raise ValueError("No default Orion platform.")

        self.logger.debug("Connecting to Orion platform: {}".format(platform))

        try:
            self.client = SwisClient(
                self.config['orion'][platform]['host'],
                self.config['orion'][platform]['user'],
                self.config['orion'][platform]['password'])
        except KeyError:
            raise ValueError("Orion host details not in the config.yaml")

        return platform

    def get_node(self, node):
        """
        Get an OrionNode object
        """

        orion_node = OrionNode()

        if is_ip(node):
            query_for_where = "IPAddress"
        else:
            query_for_where = "Caption"

        swql = """SELECT NodeID, Uri, IPAddress, Caption
        FROM Orion.Nodes
        WHERE {}=@query_on""".format(query_for_where)
        kargs = {'query_on': node}
        data = self.query(swql, **kargs)

        if not 'results' in data:
            msg = "No results from Orion: {}".format(data)
            self.logger.info(msg)
            raise Exception(msg)

        if len(data['results']) == 1:
            try:
                orion_node.npm_id = data['results'][0]['NodeID']
                orion_node.uri = data['results'][0]['Uri']
                orion_node.ip_address = data['results'][0]['IPAddress']
                orion_node.caption = data['results'][0]['Caption']
            except IndexError:
                pass
        elif len(data['results']) >= 2:
            self.logger.debug(
                "Muliple Nodes match '{}' Caption: {}".format(
                    node, data))
            raise ValueError("Muliple Nodes match '{}' Caption".format(
                node))

        if orion_node.npm:
            swql = """SELECT NodeID
            FROM Cirrus.Nodes
            WHERE CoreNodeID=@CoreNodeID"""
            kargs = {'CoreNodeID': orion_node.npm_id}
            data = self.query(swql, **kargs)

            # Don't raise an exception if this fails.
            # The platform may not haev NCM installed.
            if not 'results' in data:
                msg = "No results from Orion NCM: {}".format(data)
                self.logger.info(msg)
            elif len(data['results']) == 1:
                try:
                    orion_node.ncm_id = data['results'][0]['NodeID']
                except IndexError:
                    pass

        return orion_node

    def query(self, swql, **kargs):
        """
        Run SWQL against the Orion Platform.
        """
        return self.client.query(swql, **kargs)

    def invoke(self, entity, verb, *args):
        """
        Run an Invoke against the Orion Platform.
        """
        return self.client.invoke(entity, verb, *args)

    def create(self, entity, **kargs):
        """
        Run an Create against the Orion Platform.
        """
        return self.client.create(entity, **kargs)

    def read(self, uri):
        """
        Run an Read against the Orion Platform.
        """
        return self.client.read(uri)

    def update(self, uri, **kargs):
        """
        Run an Update against the Orion Platform.
        """
        return self.client.update(uri, **kargs)

    def delete(self, uri):
        """
        Run an Delete of an URI against the Orion Platform.
        """
        return self.client.delete(uri)

    def get_snmp_community(self, community, std_community):
        """
        Return the correct SNMP comminity to use.
        """
        if community is not None:
            return community
        elif std_community is not None:
            try:
                return self.config['defaults']['snmp'][std_community]
            except KeyError:
                raise ValueError("Invalid standard community")
        elif std_community is None:
            raise ValueError("Need one of community or std_community")

    def get_snmp_cred_id(self, community):
        """
        Look up an SNMP community in the config and then look up
        the Orion ID for the Credential.
        """

        # Check if community is a know standard, otherwise
        # use it as the community.
        try:
            name = self.get_snmp_community(None, community)
        except ValueError:
            name = community

        swql = """SELECT ID FROM Orion.Credential
        WHERE CredentialType=@CredentialType and Name=@name"""

        kargs = {'CredentialType':
                 'SolarWinds.Orion.Core.Models.Credentials.SnmpCredentialsV2',
                 'name': name}
        orion_data = self.query(swql, **kargs)

        if len(orion_data['results']) == 1:
            return orion_data['results'][0]['ID']
        else:
            raise ValueError(
                "Failed to lookup community in Orion.Credential!")

    def get_engine_id(self, poller):
        """
        Takes a poller name (or primary) and returns the EngineID for
        the poller.

        Raises: ValueError on an invaild poller.

        Returns: The EngineID (int)
        """

        if poller == "primary":
            return 1
        else:
            swql = """SELECT EngineID, ServerName, IP, ServerType
            FROM Orion.Engines
            WHERE ServerName=@poller"""
            kargs = {'poller': poller}
            data = self.query(swql, **kargs)

            if len(data['results']) == 1:
                return data['results'][0]['EngineID']
            else:
                send_user_error("Invalid poller name")
                raise ValueError("Invalid poller name")

    def get_ncm_transfer_results(self, transfer_id, sleep_delay=10):
        """
        Gets the completed (waits until finished). NCM job transfer status
        from Orion.

        Retruns: The completed status.
        """
        ts = {}
        while True:
            swql = """SELECT TransferID, NodeID, Action, RequestedConfigType,
            RequestedScript, RequestedReboot, ConfigID, TransferProtocol,
            Status, ErrorMessage, DeviceOutput, DateTime, UserName
            FROM NCM.TransferResults
            WHERE TransferID=@transfer_id"""
            kargs = {'transfer_id': transfer_id}

            transfer_data = self.query(swql, **kargs)
            status = transfer_data['results'][0]['Status']

            if status == 1:
                time.sleep(sleep_delay)
            elif status == 2:
                ts['status'] = "Complete"
                break
            elif status == 3:
                ts['status'] = "Error"
                break
            else:
                ts['status'] = "Unknown"
                break

        ts['RequestedScript'] = transfer_data['results'][0]['RequestedScript']
        ts['RequestedReboot'] = transfer_data['results'][0]['RequestedReboot']
        ts['ErrorMessage'] = transfer_data['results'][0]['ErrorMessage']
        ts['DeviceOutput'] = transfer_data['results'][0]['DeviceOutput']
        ts['UserName'] = transfer_data['results'][0]['UserName']

        return ts
