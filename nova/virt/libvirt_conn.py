# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
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
A connection to a hypervisor (e.g. KVM) through libvirt.
"""

import json
import logging
import os.path
import shutil

from twisted.internet import defer
from twisted.internet import task

from nova import exception
from nova import flags
from nova import process
from nova import utils
from nova.auth import manager
from nova.compute import disk
from nova.compute import instance_types
from nova.compute import power_state
from nova.virt import images

libvirt = None
libxml2 = None


FLAGS = flags.FLAGS
flags.DEFINE_string('libvirt_xml_template',
                    utils.abspath('virt/libvirt.qemu.xml.template'),
                    'Libvirt XML Template for QEmu/KVM')
flags.DEFINE_string('libvirt_uml_xml_template',
                    utils.abspath('virt/libvirt.uml.xml.template'),
                    'Libvirt XML Template for user-mode-linux')
flags.DEFINE_string('injected_network_template',
                    utils.abspath('virt/interfaces.template'),
                    'Template file for injected network')
flags.DEFINE_string('libvirt_type',
                    'kvm',
                    'Libvirt domain type (valid options are: kvm, qemu, uml)')
flags.DEFINE_string('libvirt_uri',
                    '',
                    'Override the default libvirt URI (which is dependent'
                    ' on libvirt_type)')


def get_connection(read_only):
    # These are loaded late so that there's no need to install these
    # libraries when not using libvirt.
    global libvirt
    global libxml2
    if libvirt is None:
        libvirt = __import__('libvirt')
    if libxml2 is None:
        libxml2 = __import__('libxml2')
    return LibvirtConnection(read_only)


class LibvirtConnection(object):
    def __init__(self, read_only):
        self.libvirt_uri, template_file = self.get_uri_and_template()

        self.libvirt_xml = open(template_file).read()
        self._wrapped_conn = None
        self.read_only = read_only

    @property
    def _conn(self):
        if not self._wrapped_conn:
            self._wrapped_conn = self._connect(self.libvirt_uri, self.read_only)
        return self._wrapped_conn

    def get_uri_and_template(self):
        if FLAGS.libvirt_type == 'uml':
            uri = FLAGS.libvirt_uri or 'uml:///system'
            template_file = FLAGS.libvirt_uml_xml_template
        else:
            uri = FLAGS.libvirt_uri or 'qemu:///system'
            template_file = FLAGS.libvirt_xml_template
        return uri, template_file

    def _connect(self, uri, read_only):
        auth = [[libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_NOECHOPROMPT],
                'root',
                None]

        if read_only:
            return libvirt.openReadOnly(uri)
        else:
            return libvirt.openAuth(uri, auth, 0)

    def list_instances(self):
        return [self._conn.lookupByID(x).name()
                for x in self._conn.listDomainsID()]

    def destroy(self, instance):
        try:
            virt_dom = self._conn.lookupByName(instance.name)
            virt_dom.destroy()
        except Exception as _err:
            pass
            # If the instance is already terminated, we're still happy
        d = defer.Deferred()
        d.addCallback(lambda _: self._cleanup(instance))
        # FIXME: What does this comment mean?
        # TODO(termie): short-circuit me for tests
        # WE'LL save this for when we do shutdown,
        # instead of destroy - but destroy returns immediately
        timer = task.LoopingCall(f=None)
        def _wait_for_shutdown():
            try:
                instance.set_state(self.get_info(instance.name)['state'])
                if instance.state == power_state.SHUTDOWN:
                    timer.stop()
                    d.callback(None)
            except Exception:
                instance.set_state(power_state.SHUTDOWN)
                timer.stop()
                d.callback(None)
        timer.f = _wait_for_shutdown
        timer.start(interval=0.5, now=True)
        return d

    def _cleanup(self, instance):
        target = os.path.join(FLAGS.instances_path, instance.name)
        logging.info("Deleting instance files at %s", target)
        if os.path.exists(target):
            shutil.rmtree(target)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def reboot(self, instance):
        xml = self.to_xml(instance)
        yield self._conn.lookupByName(instance.name).destroy()
        yield self._conn.createXML(xml, 0)

        d = defer.Deferred()
        timer = task.LoopingCall(f=None)
        def _wait_for_reboot():
            try:
                instance.set_state(self.get_info(instance.name)['state'])
                if instance.state == power_state.RUNNING:
                    logging.debug('rebooted instance %s' % instance.name)
                    timer.stop()
                    d.callback(None)
            except Exception, exn:
                logging.error('_wait_for_reboot failed: %s' % exn)
                instance.set_state(power_state.SHUTDOWN)
                timer.stop()
                d.callback(None)
        timer.f = _wait_for_reboot
        timer.start(interval=0.5, now=True)
        yield d

    @defer.inlineCallbacks
    @exception.wrap_exception
    def spawn(self, instance):
        xml = self.to_xml(instance)
        instance.set_state(power_state.NOSTATE, 'launching')
        yield self._create_image(instance, xml)
        yield self._conn.createXML(xml, 0)
        # TODO(termie): this should actually register
        # a callback to check for successful boot
        logging.debug("Instance is running")

        local_d = defer.Deferred()
        timer = task.LoopingCall(f=None)
        def _wait_for_boot():
            try:
                instance.set_state(self.get_info(instance.name)['state'])
                if instance.state == power_state.RUNNING:
                    logging.debug('booted instance %s' % instance.name)
                    timer.stop()
                    local_d.callback(None)
            except:
                logging.exception('Failed to boot instance %s' % instance.name)
                instance.set_state(power_state.SHUTDOWN)
                timer.stop()
                local_d.callback(None)
        timer.f = _wait_for_boot
        timer.start(interval=0.5, now=True)
        yield local_d

    @defer.inlineCallbacks
    def _create_image(self, inst, libvirt_xml):
        # syntactic nicety
        basepath = lambda x='': os.path.join(FLAGS.instances_path, inst.name, x)

        # ensure directories exist and are writable
        yield process.simple_execute('mkdir -p %s' % basepath())
        yield process.simple_execute('chmod 0777 %s' % basepath())


        # TODO(termie): these are blocking calls, it would be great
        #               if they weren't.
        logging.info('Creating image for: %s', inst.name)
        f = open(basepath('libvirt.xml'), 'w')
        f.write(libvirt_xml)
        f.close()

        user = manager.AuthManager().get_user(inst.user_id)
        project = manager.AuthManager().get_project(inst.project_id)
        if not os.path.exists(basepath('disk')):
           yield images.fetch(inst.image_id, basepath('disk-raw'), user, project)
        if not os.path.exists(basepath('kernel')):
           yield images.fetch(inst.kernel_id, basepath('kernel'), user, project)
        if not os.path.exists(basepath('ramdisk')):
           yield images.fetch(inst.ramdisk_id, basepath('ramdisk'), user, project)

        execute = lambda cmd, process_input=None: \
                  process.simple_execute(cmd=cmd,
                                         process_input=process_input,
                                         check_exit_code=True)

        key = inst.key_data
        net = None
        network = inst.project.network
        if False: # should be network.is_injected:
            with open(FLAGS.injected_network_template) as f:
                net = f.read() % {'address': inst.fixed_ip,
                                  'network': network.network,
                                  'netmask': network.netmask,
                                  'gateway': network.gateway,
                                  'broadcast': network.broadcast,
                                  'dns': network.network.dns}
        if key or net:
            logging.info('Injecting data into image %s', inst.image_id)
            yield disk.inject_data(basepath('disk-raw'), key, net, execute=execute)

        if os.path.exists(basepath('disk')):
            yield process.simple_execute('rm -f %s' % basepath('disk'))

        bytes = (instance_types.INSTANCE_TYPES[inst.instance_type]['local_gb']
                 * 1024 * 1024 * 1024)
        yield disk.partition(
                basepath('disk-raw'), basepath('disk'), bytes, execute=execute)

    def to_xml(self, instance):
        # TODO(termie): cache?
        logging.debug("Starting the toXML method")
        network = instance.project.network
        # FIXME(vish): stick this in db
        instance_type = instance_types.INSTANCE_TYPES[instance.instance_type]
        xml_info = {'type': FLAGS.libvirt_type,
                    'name': instance.name,
                    'basepath': os.path.join(FLAGS.instances_path, instance.name),
                    'memory_kb': instance_type['memory_mb'] * 1024,
                    'vcpus': instance_type['vcpus'],
                    'bridge_name': network.bridge_name,
                    'mac_address': instance.mac_address}
        libvirt_xml = self.libvirt_xml % xml_info
        logging.debug("Finished the toXML method")

        return libvirt_xml

    def get_info(self, instance_name):
        virt_dom = self._conn.lookupByName(instance_name)
        (state, max_mem, mem, num_cpu, cpu_time) = virt_dom.info()
        return {'state': state,
                'max_mem': max_mem,
                'mem': mem,
                'num_cpu': num_cpu,
                'cpu_time': cpu_time}

    def get_disks(self, instance_name):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.

        Returns a list of all block devices for this domain.
        """
        domain = self._conn.lookupByName(instance_name)
        # TODO(devcamcar): Replace libxml2 with etree.
        xml = domain.XMLDesc(0)
        doc = None

        try:
            doc = libxml2.parseDoc(xml)
        except:
            return []

        ctx = doc.xpathNewContext()
        disks = []

        try:
            ret = ctx.xpathEval('/domain/devices/disk')

            for node in ret:
                devdst = None

                for child in node.children:
                    if child.name == 'target':
                        devdst = child.prop('dev')

                if devdst == None:
                    continue

                disks.append(devdst)
        finally:
            if ctx != None:
                ctx.xpathFreeContext()
            if doc != None:
                doc.freeDoc()

        return disks

    def get_interfaces(self, instance_name):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.

        Returns a list of all network interfaces for this instance.
        """
        domain = self._conn.lookupByName(instance_name)
        # TODO(devcamcar): Replace libxml2 with etree.
        xml = domain.XMLDesc(0)
        doc = None

        try:
            doc = libxml2.parseDoc(xml)
        except:
            return []

        ctx = doc.xpathNewContext()
        interfaces = []

        try:
            ret = ctx.xpathEval('/domain/devices/interface')

            for node in ret:
                devdst = None

                for child in node.children:
                    if child.name == 'target':
                        devdst = child.prop('dev')

                if devdst == None:
                    continue

                interfaces.append(devdst)
        finally:
            if ctx != None:
                ctx.xpathFreeContext()
            if doc != None:
                doc.freeDoc()

        return interfaces

    def block_stats(self, instance_name, disk):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.
        """
        domain = self._conn.lookupByName(instance_name)
        return domain.blockStats(disk)

    def interface_stats(self, instance_name, interface):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.
        """
        domain = self._conn.lookupByName(instance_name)
        return domain.interfaceStats(interface)
