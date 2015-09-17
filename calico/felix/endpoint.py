# -*- coding: utf-8 -*-
# Copyright (c) 2015 Metaswitch Networks
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
"""
felix.endpoint
~~~~~~~~~~~~~

Endpoint management.
"""
import logging
from calico.felix import devices, futils
from calico.felix.actor import actor_message
from calico.felix.futils import FailedSystemCall
from calico.felix.futils import IPV4
from calico.felix.refcount import ReferenceManager, RefCountedActor, RefHelper
from calico.felix.dispatch import DispatchChains
from calico.felix.profilerules import RulesManager
from calico.felix.frules import (
    profile_to_chain_name, commented_drop_fragment, interface_to_suffix,
    chain_names
)

_log = logging.getLogger(__name__)


class EndpointManager(ReferenceManager):
    def __init__(self, config, ip_type,
                 iptables_updater,
                 dispatch_chains,
                 rules_manager,
                 datastore_api):
        super(EndpointManager, self).__init__(qualifier=ip_type)

        # Configuration and version to use
        self.config = config
        self.ip_type = ip_type
        self.ip_version = futils.IP_TYPE_TO_VERSION[ip_type]

        # Peers/utility classes.
        self.iptables_updater = iptables_updater
        self.dispatch_chains = dispatch_chains
        self.rules_mgr = rules_manager
        self.datastore_api = datastore_api

        # All endpoint dicts that are on this host.
        self.endpoints_by_id = {}
        # Dict that maps from interface name ("tap1234") to endpoint ID.
        self.endpoint_id_by_iface_name = {}

        # Set of endpoints that are live on this host.  I.e. ones that we've
        # increffed.
        self.local_endpoint_ids = set()

    def _create(self, combined_id):
        """
        Overrides ReferenceManager._create()
        """
        return LocalEndpoint(self.config,
                             combined_id,
                             self.ip_type,
                             self.iptables_updater,
                             self.dispatch_chains,
                             self.rules_mgr,
                             self.datastore_api)

    def _on_object_started(self, endpoint_id, obj):
        """
        Callback from a LocalEndpoint to report that it has started.
        Overrides ReferenceManager._on_object_started
        """
        ep = self.endpoints_by_id.get(endpoint_id)
        obj.on_endpoint_update(ep, async=True)

    @actor_message()
    def apply_snapshot(self, endpoints_by_id):
        # Tell the dispatch chains about the local endpoints in advance so
        # that we don't flap the dispatch chain at start-of-day.
        local_iface_name_to_ep_id = {}
        for ep_id, ep in endpoints_by_id.iteritems():
            if ep and ep_id.host == self.config.HOSTNAME and ep.get("name"):
                local_iface_name_to_ep_id[ep.get("name")] = ep_id
        self.dispatch_chains.apply_snapshot(local_iface_name_to_ep_id.keys(),
                                            async=True)
        # Then update/create endpoints and work out which endpoints have been
        # deleted.
        missing_endpoints = set(self.endpoints_by_id.keys())
        for endpoint_id, endpoint in endpoints_by_id.iteritems():
            self.on_endpoint_update(endpoint_id, endpoint,
                                    force_reprogram=True)
            missing_endpoints.discard(endpoint_id)
            self._maybe_yield()
        for endpoint_id in missing_endpoints:
            self.on_endpoint_update(endpoint_id, None)
            self._maybe_yield()

    @actor_message()
    def on_endpoint_update(self, endpoint_id, endpoint, force_reprogram=False):
        """
        Event to indicate that an endpoint has been updated (including
        creation or deletion).

        :param EndpointId endpoint_id: The endpoint ID in question.
        :param dict[str]|NoneType endpoint: Dictionary of all endpoint
            data or None if the endpoint is to be deleted.
        """
        if endpoint_id.host != self.config.HOSTNAME:
            _log.debug("Skipping endpoint %s; not on our host.", endpoint_id)
            return

        if self._is_starting_or_live(endpoint_id):
            # Local endpoint thread is running; tell it of the change.
            _log.info("Update for live endpoint %s", endpoint_id)
            self.objects_by_id[endpoint_id].on_endpoint_update(
                endpoint, force_reprogram=force_reprogram, async=True)

        old_ep = self.endpoints_by_id.pop(endpoint_id, {})
        # Interface name shouldn't change but popping it now is correct for
        # deletes and we add it back in below on create/modify.
        old_iface_name = old_ep.get("name")
        self.endpoint_id_by_iface_name.pop(old_iface_name, None)
        if endpoint is None:
            # Deletion. Remove from the list.
            _log.info("Endpoint %s deleted", endpoint_id)
            if endpoint_id in self.local_endpoint_ids:
                self.decref(endpoint_id)
                self.local_endpoint_ids.remove(endpoint_id)
        else:
            # Creation or modification
            _log.info("Endpoint %s modified or created", endpoint_id)
            self.endpoints_by_id[endpoint_id] = endpoint
            self.endpoint_id_by_iface_name[endpoint["name"]] = endpoint_id
            if endpoint_id not in self.local_endpoint_ids:
                # This will trigger _on_object_activated to pass the endpoint
                # we just saved off to the endpoint.
                self.local_endpoint_ids.add(endpoint_id)
                self.get_and_incref(endpoint_id)

    @actor_message()
    def on_interface_update(self, name):
        """
        Called when an interface is created or changes state.

        The interface may be any interface on the host, not necessarily
        one managed by any endpoint of this server.
        """
        try:
            endpoint_id = self.endpoint_id_by_iface_name[name]
        except KeyError:
            _log.debug("Update on interface %s that we do not care about",
                       name)
        else:
            _log.info("Endpoint %s received interface update for %s",
                      endpoint_id, name)
            if self._is_starting_or_live(endpoint_id):
                # LocalEndpoint is running, so tell it about the change.
                ep = self.objects_by_id[endpoint_id]
                ep.on_interface_update(async=True)


class LocalEndpoint(RefCountedActor):

    def __init__(self, config, combined_id, ip_type, iptables_updater,
                 dispatch_chains, rules_manager, datastore_api):
        """
        Controls a single local endpoint.

        :param combined_id: EndpointId for this endpoint.
        :param ip_type: IP type for this endpoint (IPv4 or IPv6)
        :param iptables_updater: IptablesUpdater to use
        :param dispatch_chains: DispatchChains to use
        :param rules_manager: RulesManager to use
        """
        super(LocalEndpoint, self).__init__(qualifier="%s(%s)" %
                                            (combined_id.endpoint, ip_type))
        assert isinstance(dispatch_chains, DispatchChains)
        assert isinstance(rules_manager, RulesManager)

        self.config = config

        self.combined_id = combined_id
        self.ip_type = ip_type

        self.iptables_updater = iptables_updater
        self.dispatch_chains = dispatch_chains
        self.rules_mgr = rules_manager

        self.rules_ref_helper = RefHelper(self, rules_manager,
                                          self._on_profiles_ready)
        self.datastore_api = datastore_api

        self._pending_endpoint = None
        self._endpoint_update_pending = False
        self._mac_changed = False

        # Will be filled in as we learn about the OS interface and the
        # endpoint config.
        self.endpoint = None
        self._mac = None
        self._iface_name = None
        self._suffix = None

        # Keep track of which dependencies we're missing.
        self._missing_deps = self._calculate_missing_deps()

        # Track the success/failure of our dataplane programming.
        self._iptables_in_sync = False
        self._device_in_sync = False

        # One-way flags to indicate that we should clean up/have cleaned up.
        self._unreferenced = False
        self._added_to_dispatch_chains = False
        self._cleaned_up = False

    @property
    def nets_key(self):
        if self.ip_type == IPV4:
            return "ipv4_nets"
        else:
            return "ipv6_nets"

    @actor_message()
    def on_endpoint_update(self, endpoint, force_reprogram=False):
        """
        Called when this endpoint has received an update.
        :param dict[str]|NoneType endpoint: endpoint parameter dictionary.
        """
        _log.info("%s updated: %s", self, endpoint)
        assert not self._unreferenced, "Update after being unreferenced"

        # Store off the update, to be handled in _finish_msg_batch.
        self._pending_endpoint = endpoint
        self._endpoint_update_pending = True
        if force_reprogram:
            self._iptables_in_sync = False
            self._device_in_sync = False

    @actor_message()
    def on_interface_update(self):
        """
        Actor event to report that the interface is either up or changed.
        """
        _log.info("Endpoint %s received interface kick", self.combined_id)

        # Use a flag so that we coalesce any duplicate updates in
        # _finish_msg_batch.
        self._device_in_sync = False

    @actor_message()
    def on_unreferenced(self):
        """
        Overrides RefCountedActor:on_unreferenced.
        """
        _log.info("%s now unreferenced, cleaning up", self)

        # We should be deleted before being unreferenced.
        assert self.endpoint is None or (self._pending_endpoint is None and
                                         self._endpoint_update_pending)

        # Defer the processing to _finish_msg_batch.
        self._unreferenced = True

    def _start_msg_batch(self, batch):
        self._in_sync_at_start_of_batch = (self._iptables_in_sync and
                                           self._device_in_sync)
        return super(LocalEndpoint, self)._start_msg_batch(batch)

    def _finish_msg_batch(self, batch, results):
        if self._cleaned_up:
            # We could just ignore this but it suggests that the
            # EndpointManager is bugged.
            raise AssertionError(
                "Unexpected update to %s (%s) after being unreferenced" %
                (self, self.__dict__)
            )

        if self._endpoint_update_pending:
            # Copy the pending update into our data structures.  May work out
            # that iptables or the device is now out of sync.
            _log.debug("Endpoint update pending: %s", self._pending_endpoint)
            self._apply_endpoint_update()

        if not self._iptables_in_sync:
            # Try to update iptables, if successful, will set the
            # _iptables_in_sync flag.
            _log.debug("iptables is out-of-sync, trying to update it")
            self._maybe_update_iptables()

        if not self._device_in_sync and self._iface_name:
            # Try to update the device configuration.  If successful, will set
            # the _device_in_sync flag.
            if self.endpoint:
                # Endpoint is supposed to be live, try to configure it.
                _log.debug("Device is out-of-sync, trying to configure it")
                self._configure_interface()
            else:
                # We've been deleted, de-configure the interface.
                _log.debug("Device is out-of-sync, trying to de-configure it")
                self._deconfigure_interface()

        if self._unreferenced:
            # Endpoint is being removed, clean up...
            _log.debug("Cleaning up after endpoint unreferenced")
            self.dispatch_chains.on_endpoint_removed(self._iface_name,
                                                     async=True)
            self.rules_ref_helper.discard_all()
            self._notify_cleanup_complete()
            self._cleaned_up = True
        elif not self._added_to_dispatch_chains:
            # This must be the first batch, add ourself to the dispatch chains.
            _log.debug("Adding endpoint to dispatch chain")
            self.dispatch_chains.on_endpoint_added(self._iface_name,
                                                   async=True)
            self._added_to_dispatch_chains = True

        in_sync_at_end_of_batch = (self._iptables_in_sync and
                                   self._device_in_sync)

        if self._unreferenced or (self._in_sync_at_start_of_batch !=
                                  in_sync_at_end_of_batch):
            if self._unreferenced:
                _log.debug("Unreferenced, reporting status = None")
                status = None
            else:
                _log.debug("Endpoint oper state changed to %s",
                           "up" if in_sync_at_end_of_batch else "down")
                status = {"status": "up" if in_sync_at_end_of_batch
                                    else "down"}
            self.datastore_api.on_endpoint_status_changed(
                self._id,
                status,
                async=True,
            )

    def _apply_endpoint_update(self):
        pending_endpoint = self._pending_endpoint
        if pending_endpoint == self.endpoint:
            _log.debug("Endpoint hasn't changed, nothing to do")
            return

        if pending_endpoint:
            # Update/create.
            if pending_endpoint['mac'] != self._mac:
                # Either we have not seen this MAC before, or it has changed.
                _log.debug("Endpoint MAC changed to %s",
                           pending_endpoint["mac"])
                self._mac = pending_endpoint['mac']
                self._mac_changed = True
                # MAC change requires refresh of iptables rules and ARP table.
                self._iptables_in_sync = False
                self._device_in_sync = False

            if self.endpoint is None:
                # This is the first time we have seen the endpoint, so extract
                # the interface name and endpoint ID.
                self._iface_name = pending_endpoint["name"]
                self._suffix = interface_to_suffix(self.config,
                                                   self._iface_name)
                _log.debug("Learned interface name/suffix: %s/%s",
                           self._iface_name, self._suffix)
                # First time through, need to program everything.
                self._iptables_in_sync = False
                self._device_in_sync = False

            # Check if the profile ID or IP addresses have changed, requiring
            # a refresh of the dataplane.
            profile_ids = set(pending_endpoint.get("profile_ids", []))
            if profile_ids != self.rules_ref_helper.required_refs:
                # Profile ID update required iptables update but not device
                # update.
                _log.debug("Profile IDs changed, need to update iptables")
                self._iptables_in_sync = False
            if (self.endpoint and
                    (self.endpoint[self.nets_key] !=
                     pending_endpoint[self.nets_key])):
                # IP addresses have changed, need to update the routing table.
                _log.debug("IP addresses changed, need to update routing")
                self._device_in_sync = False
        else:
            # Delete of the endpoint.  Need to resync everything.
            profile_ids = set()
            self._iptables_in_sync = False
            self._device_in_sync = False

        # Note: we don't actually need to wait for the activation to finish
        # due to the dependency management in the iptables layer.
        self.rules_ref_helper.replace_all(profile_ids)

        self.endpoint = pending_endpoint
        self._endpoint_update_pending = False
        self._pending_endpoint = None

    def _calculate_missing_deps(self):
        """
        Returns a list of missing dependencies.
        """
        missing_deps = []
        if not self.endpoint:
            missing_deps.append("endpoint")
        elif self.endpoint.get("state", "active") != "active":
            missing_deps.append("endpoint active")
        elif not self.endpoint.get("profile_ids"):
            missing_deps.append("profile")
        return missing_deps

    def _maybe_update_iptables(self):
        """
        Update the relevant programming for this endpoint.
        """
        old_missing_deps = self._missing_deps
        self._missing_deps = self._calculate_missing_deps()

        if not self._missing_deps:
            # We have all the dependencies we need to do the programming and
            # the caller has already worked out that iptables needs refreshing.
            _log.info("%s became ready to program.", self)
            self._update_chains()
        elif not old_missing_deps and self._missing_deps:
            # We were active but now we're not, withdraw the dispatch rule
            # and our chain.  We must do this to allow iptables to remove
            # the profile chain when we're being deleted.
            _log.debug("%s not ready, waiting on %s", self, self._missing_deps)
            _log.info("%s not ready.", self)
            self._remove_chains()

    def _update_chains(self):
        updates, deps = _get_endpoint_rules(self.combined_id.endpoint,
                                            self._suffix,
                                            self._mac,
                                            self.endpoint["profile_ids"])
        try:
            self.iptables_updater.rewrite_chains(updates, deps, async=False)
        except FailedSystemCall:
            _log.exception("Failed to program chains for %s. Removing.", self)
            try:
                self.iptables_updater.delete_chains(chain_names(self._suffix),
                                                    async=False)
            except FailedSystemCall:
                _log.exception("Failed to remove chains after original "
                               "failure")
        else:
            self._iptables_in_sync = True

    def _remove_chains(self):
        try:
            self.iptables_updater.delete_chains(chain_names(self._suffix),
                                                async=True)
        except FailedSystemCall:
            _log.exception("Failed to delete chains for %s", self)
        else:
            self._iptables_in_sync = True

    def _configure_interface(self):
        """
        Applies sysctls and routes to the interface.

        :param: bool mac_changed: Has the MAC address changed since it was last
                     configured? If so, we reconfigure ARP for the interface in
                     IPv4 (ARP does not exist for IPv6, which uses neighbour
                     solicitation instead).
        """
        try:
            if self.ip_type == IPV4:
                devices.configure_interface_ipv4(self._iface_name)
                reset_arp = self._mac_changed
            else:
                ipv6_gw = self.endpoint.get("ipv6_gateway", None)
                devices.configure_interface_ipv6(self._iface_name, ipv6_gw)
                reset_arp = False

            ips = set()
            for ip in self.endpoint.get(self.nets_key, []):
                ips.add(futils.net_to_ip(ip))
            devices.set_routes(self.ip_type, ips,
                               self._iface_name,
                               self.endpoint["mac"],
                               reset_arp=reset_arp)

        except (IOError, FailedSystemCall):
            if not devices.interface_exists(self._iface_name):
                _log.info("Interface %s for %s does not exist yet",
                          self._iface_name, self.combined_id)
            elif not devices.interface_up(self._iface_name):
                _log.info("Interface %s for %s is not up yet",
                          self._iface_name, self.combined_id)
            else:
                # Interface flapped back up after we failed?
                _log.warning("Failed to configure interface %s for %s",
                             self._iface_name, self.combined_id)
        else:
            _log.info("Interface %s configured", self._iface_name)
            self._device_in_sync = True

    def _deconfigure_interface(self):
        """
        Removes routes from the interface.
        """
        try:
            devices.set_routes(self.ip_type, set(), self._iface_name, None)
        except (IOError, FailedSystemCall):
            if not devices.interface_exists(self._iface_name):
                # Deleted under our feet - so the rules are gone.
                _log.debug("Interface %s for %s deleted",
                           self._iface_name, self.combined_id)
            else:
                # An error deleting the routes. Log and continue.
                _log.exception("Cannot delete routes for interface %s for %s",
                               self._iface_name, self.combined_id)
        else:
            _log.info("Interface %s deconfigured", self._iface_name)
            self._device_in_sync = True

    def _on_profiles_ready(self):
        # We don't actually need to talk to the profiles, just log.
        _log.info("Endpoint %s acquired all required profile references",
                  self.combined_id)

    def __str__(self):
        return ("LocalEndpoint<%s,id=%s,iface=%s>" %
                (self.ip_type, self.combined_id,
                 self._iface_name or "unknown"))


def _get_endpoint_rules(endpoint_id, suffix, mac, profile_ids):
    to_chain_name, from_chain_name = chain_names(suffix)

    to_chain, to_deps = _build_to_or_from_chain(
        endpoint_id,
        profile_ids,
        to_chain_name,
        "inbound"
    )
    from_chain, from_deps = _build_to_or_from_chain(
        endpoint_id,
        profile_ids,
        from_chain_name,
        "outbound",
        expected_mac=mac,
    )

    updates = {to_chain_name: to_chain, from_chain_name: from_chain}
    deps = {to_chain_name: to_deps, from_chain_name: from_deps}
    return updates, deps


def _build_to_or_from_chain(endpoint_id, profile_ids, chain_name,
                            direction, expected_mac=None):
    # Ensure the MARK is set to 0 when we start so that unmatched packets will
    # be dropped.
    chain = [
        "--append %s --jump MARK --set-mark 0" % chain_name
    ]
    if expected_mac:
        _log.debug("Policing source MAC: %s", expected_mac)
        chain.append('--append %s --match mac ! --mac-source %s --jump DROP '
                     '--match comment --comment "Incorrect source MAC"' %
                     (chain_name, expected_mac))
    # Jump to each profile in turn.  The profile will do one of the
    # following:
    # * DROP the packet; in which case we won't see it again.
    # * RETURN the packet with MARK==1, indicating it accepted the packet. In
    #   which case, we RETURN and skip further profiles.
    # * RETURN the packet with MARK==0, indicating it did not match the packet.
    #   In which case, we carry on and process the next profile.
    deps = set()
    for profile_id in profile_ids:
        profile_chain = profile_to_chain_name(direction, profile_id)
        deps.add(profile_chain)
        chain.append("--append %s --jump %s" % (chain_name, profile_chain))
        # If the profile accepted the packet, it sets MARK==1.  Immediately
        # RETURN the packet to signal that it's been accepted.
        chain.append('--append %s --match mark --mark 1/1 '
                     '--match comment --comment "Profile accepted packet" '
                     '--jump RETURN' % chain_name)

    # Default drop rule.
    chain.append(
        commented_drop_fragment(
            chain_name,
            "Default DROP if no match (endpoint %s):" % endpoint_id
        )
    )
    return chain, deps
