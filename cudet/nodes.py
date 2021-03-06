#    Copyright 2016 Mirantis, Inc.
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

import copy
import datetime
import logging
import json
import os
import shutil
import sys

from collections import Iterable

from cudet import configuration
from cudet import exceptions
from cudet import fuel_client
from cudet import utils
from six import string_types


logger = logging.getLogger(__name__)


class Node(object):
    ckey = 'cmds'
    skey = 'scripts'
    fkey = 'files'
    flkey = 'filelists'
    lkey = 'logs'
    pkey = 'put'
    conf_actionable = [lkey, ckey, skey, fkey, flkey, pkey]
    conf_appendable = [lkey, ckey, skey, fkey, flkey, pkey]
    conf_archive_general = [ckey, skey, fkey, flkey]
    conf_keep_default = [skey, ckey, fkey, flkey]
    conf_once_prefix = 'once_'
    conf_match_prefix = 'by_'
    conf_default_key = '__default'
    conf_priority_section = conf_match_prefix + 'id'
    header = ['node-id', 'env', 'ip', 'mac', 'os',
              'roles', 'online', 'status', 'name', 'fqdn']

    def __init__(self, id, name, fqdn, mac, cluster, release, roles,
                 os_platform, online, status, ip, conf, logger=None):
        self.id = id
        self.mac = mac
        self.cluster = cluster
        self.roles = roles
        self.os_platform = os_platform
        self.online = online
        self.status = status
        self.ip = ip
        self.release = release
        self.files = []
        self.filelists = []
        self.cmds = []
        self.scripts = []
        # put elements must be tuples - (src, dst)
        self.put = []
        self.data = {}
        self.logsize = 0
        self.mapcmds = {}
        self.mapscr = {}
        self.name = name
        self.fqdn = fqdn
        self.outputs_timestamp = False
        self.outputs_timestamp_dir = None
        self.apply_conf(conf)
        self.logger = logger or logging.getLogger(__name__)

    def apply_conf(self, conf, clean=True):

        def apply(k, v, c_a, k_d, o, default=False):
            if k in c_a:
                if any([default,
                        k not in k_d and k not in o,
                        not hasattr(self, k)]):
                    setattr(self, k, copy.deepcopy(utils.w_list(v)))
                else:
                    getattr(self, k).extend(copy.deepcopy(utils.w_list(v)))
                if not default:
                    o[k] = True
            else:
                setattr(self, k, copy.deepcopy(v))

        def r_apply(el, p, p_s, c_a, k_d, o, d, clean=False):
            # apply normal attributes
            for k in [k for k in el if k != p_s and not k.startswith(p)]:
                if el == conf and clean:
                    apply(k, el[k], c_a, k_d, o, default=True)
                else:
                    apply(k, el[k], c_a, k_d, o)
            # apply match attributes (by_xxx except by_id)
            for k in [k for k in el if k != p_s and k.startswith(p)]:
                attr_name = k[len(p):]
                if hasattr(self, attr_name):
                    attr = utils.w_list(getattr(self, attr_name))
                    for v in attr:
                        if v in el[k]:
                            subconf = el[k][v]
                            if d in el:
                                d_conf = el[d]
                                for a in d_conf:
                                    apply(a, d_conf[a], c_a, k_d, o)
                            r_apply(subconf, p, p_s, c_a, k_d, o, d)
            # apply priority attributes (by_id)
            if p_s in el:
                if self.id in el[p_s]:
                    p_conf = el[p_s][self.id]
                    if d in el[p_s]:
                        d_conf = el[p_s][d]
                        for k in d_conf:
                            apply(k, d_conf[k], c_a, k_d, o)
                    for k in [k for k in p_conf if k != d]:
                        apply(k, p_conf[k], c_a, k_d, o, default=True)

        p = Node.conf_match_prefix
        p_s = Node.conf_priority_section
        c_a = Node.conf_appendable
        k_d = Node.conf_keep_default
        d = Node.conf_default_key
        overridden = {}
        if clean:
            '''clean appendable keep_default params to ensure no content
            duplication if this function gets called more than once'''
            for f in set(c_a).intersection(k_d):
                setattr(self, f, [])
        r_apply(conf, p, p_s, c_a, k_d, overridden, d, clean=clean)

    def exec_cmd(self, fake=False, ok_codes=None):
        sn = 'node-%s' % self.id
        cl = 'cluster-%s' % self.cluster
        self.logger.debug('%s/%s/%s/%s' % (self.outdir, Node.ckey, cl, sn))
        ddir = os.path.join(self.outdir, Node.ckey, cl, sn)
        if self.cmds:
            utils.mdir(ddir)
        self.cmds = sorted(self.cmds)
        mapcmds = {}
        for c in self.cmds:
            for cmd in c:
                dfile = os.path.join(ddir, 'node-%s-%s-%s' %
                                     (self.id, self.ip, cmd))
                if self.outputs_timestamp:
                        dfile += self.outputs_timestamp_str
                self.logger.info('outfile: %s' % dfile)
                mapcmds[cmd] = dfile
                if not fake:
                    outs, errs, code = utils.ssh_node(ip=self.ip,
                                                      command=c[cmd],
                                                      ssh_opts=self.ssh_opts,
                                                      env_vars=self.env_vars,
                                                      timeout=self.timeout,
                                                      prefix=self.prefix)
                    self.check_code(code, 'exec_cmd', c[cmd], errs, ok_codes)
                    try:
                        with open(dfile, 'w') as df:
                            df.write(outs.encode('utf-8'))
                    except:
                        self.logger.error("can't write to file %s" %
                                          dfile)
        if self.scripts:
            utils.mdir(ddir)
        scripts = sorted(self.scripts)
        mapscr = {}
        for scr in scripts:
            if type(scr) is dict:
                env_vars = scr.values()[0]
                scr = scr.keys()[0]
            else:
                env_vars = self.env_vars
            if os.path.sep in scr:
                f = scr
            else:
                f = os.path.join(self.rqdir, Node.skey, scr)
            self.logger.info('node:%s(%s), exec: %s' % (self.id, self.ip, f))
            dfile = os.path.join(ddir, 'node-%s-%s-%s' %
                                 (self.id, self.ip, os.path.basename(f)))
            if self.outputs_timestamp:
                    dfile += self.outputs_timestamp_str
            self.logger.info('outfile: %s' % dfile)
            mapscr[scr] = dfile
            if not fake:
                outs, errs, code = utils.ssh_node(ip=self.ip,
                                                  filename=f,
                                                  ssh_opts=self.ssh_opts,
                                                  env_vars=env_vars,
                                                  timeout=self.timeout,
                                                  prefix=self.prefix)
                self.check_code(code, 'exec_cmd', 'script %s' % f, errs,
                                ok_codes)
                try:
                    with open(dfile, 'w') as df:
                        df.write(outs.encode('utf-8'))
                except:
                    self.logger.error("can't write to file %s" % dfile)
        return mapcmds, mapscr

    def exec_simple_cmd(self, cmd, timeout=15, infile=None, outfile=None,
                        fake=False, ok_codes=None, input=None):
        self.logger.info('node:%s(%s), exec: %s' % (self.id, self.ip, cmd))
        if not fake:
            outs, errs, code = utils.ssh_node(ip=self.ip,
                                              command=cmd,
                                              ssh_opts=self.ssh_opts,
                                              env_vars=self.env_vars,
                                              timeout=timeout,
                                              outputfile=outfile,
                                              ok_codes=ok_codes,
                                              input=input,
                                              prefix=self.prefix)
            self.check_code(code, 'exec_simple_cmd', cmd, errs, ok_codes)

    def check_code(self, code, func_name, cmd, err, ok_codes=None):
        if code:
            if not ok_codes or code not in ok_codes:
                self.logger.warning("id: %s, fqdn: %s, ip: %s, func: %s, "
                                    "cmd: '%s' exited %d, error: %s" %
                                    (self.id, self.fqdn, self.ip,
                                     func_name, cmd, code, err))


class NodeManager(object):
    """Class nodes """

    def __init__(self, conf, nodes_json=None, logger=None):
        self.conf = conf
        self.logger = logger or logging.getLogger(__name__)

        if conf.outputs_timestamp or conf.dir_timestamp:
            timestamp_str = datetime.datetime.now().strftime('_%F_%H-%M-%S')
            if conf.outputs_timestamp:
                conf.outputs_timestamp_str = timestamp_str
            if conf.dir_timestamp:
                conf.outdir += timestamp_str

        if conf.clean:
            shutil.rmtree(conf.outdir, ignore_errors=True)

        self.rqdir = conf.rqdir
        if not os.path.exists(self.rqdir):
            self.logger.critical(
                'NodeManager: directory %s does not exist') % self.rqdir
            sys.exit(1)
        if self.conf.rqfile:
            self._import_rq()

        self.nodes = {}
        self.nodes_filter = NodeFilter()
        self.fuel_client = fuel_client.get_client(self.conf)

        if self.fuel_client is None:
            self.cli_creds = 'OS_TENANT_NAME={tenant} OS_USERNAME={user} ' \
                             'OS_PASSWORD={password}'.\
                format(tenant=self.conf.fuel_tenant,
                       user=self.conf.fuel_user,
                       password=self.conf.fuel_pass)

        if self.nodes_filter.check_master:
            self._fuel_node_init()

        if nodes_json is not None:
            self.nodes_json = utils.load_json_file(nodes_json)
        else:
            if not self.get_nodes():
                sys.exit(4)

        self._nodes_init()

        self.nodes_reapply_conf()
        self._conf_assign_once()

    def _import_rq(self):

        def sub_is_match(el, d, p, once_p):
            if type(el) is not dict:
                return False
            checks = []
            for i in el:
                checks.append(any([i == d,
                                  i.startswith(p),
                                  i.startswith(once_p)]))
            return all(checks)

        def r_sub(attr, el, k, d, p, once_p, dst):
            match_sect = False
            if type(k) is str and (k.startswith(p) or k.startswith(once_p)):
                match_sect = True
            if k not in dst and k != attr:
                dst[k] = {}
            if d in el[k]:
                if k == attr:
                    dst[k] = el[k][d]
                elif k.startswith(p) or k.startswith(once_p):
                    dst[k][d] = {attr: el[k][d]}
                else:
                    dst[k][attr] = el[k][d]
            if k == attr:
                subks = [subk for subk in el[k] if subk != d]
                for subk in subks:
                    r_sub(attr, el[k], subk, d, p, once_p, dst)
            elif match_sect or sub_is_match(el[k], d, p, once_p):
                subks = [subk for subk in el[k] if subk != d]
                for subk in subks:
                    if el[k][subk] is not None:
                        if subk not in dst[k]:
                            dst[k][subk] = {}
                        r_sub(attr, el[k], subk, d, p, once_p, dst[k])
            else:
                dst[k][attr] = el[k]

        dst = self.conf
        src = utils.load_yaml_file(self.conf.rqfile)
        p = Node.conf_match_prefix
        once_p = Node.conf_once_prefix + p
        d = Node.conf_default_key
        for attr in src:
            r_sub(attr, src, attr, d, p, once_p, dst)

    def _fuel_node_init(self):
        if not self.conf.fuel_ip:
            self.logger.critical('NodeManager: fuel_ip is not set')
            sys.exit(7)

        fuel_release = self.get_master_release()

        fuelnode = Node(id=0,
                        cluster=0,
                        name='fuel',
                        fqdn='n/a',
                        mac='n/a',
                        os_platform='centos',
                        release=fuel_release,
                        roles=['fuel'],
                        status='ready',
                        online=True,
                        ip=self.conf.fuel_ip,
                        conf=self.conf)
        self.nodes[self.conf.fuel_ip] = fuelnode

    def get_nodes(self):
        if self.fuel_client is not None:
            return self._get_nodes_fuelclient()
        else:
            return self._get_nodes_cli()

    def _get_nodes_fuelclient(self):
        if not self.fuel_client:
            return False
        try:
            self.nodes_json = self.fuel_client.get_request('nodes')
            self.logger.debug(self.nodes_json)
            return True
        except Exception as e:
            self.logger.warning(("NodeManager: can't "
                                 "get node list from fuel client:\n%s" % (e)),
                                exc_info=True)
            return False

    def _get_nodes_cli(self):
        self.logger.info('use CLI for getting node information')

        cmd = 'fuel node list --json'

        nodes_json_str, err, code = utils.ssh_node(ip=self.conf.fuel_ip,
                                                   command=cmd,
                                                   env_vars=self.cli_creds,
                                                   ssh_opts=self.conf.ssh_opts,
                                                   timeout=self.conf.timeout)
        if code != 0:
            self.logger.warning(('NodeManager: cannot get '
                                 'fuel node list from CLI: %s') % err)
            self.nodes_json = None
            return False
        self.nodes_json = json.loads(nodes_json_str)
        return True

    def get_master_release(self):
        if self.fuel_client is not None:
            return self._get_master_release_fuel_client()
        else:
            return self._get_master_release_fuel_cli()

    def _get_master_release_fuel_client(self):
        fuel_version = None

        try:
            self.logger.info('getting release using fuelclient')
            v = self.fuel_client.get_request('version')
            fuel_version = v['release']
            self.logger.debug('version response:%s' % v)
        except Exception as e:
            self.logger.warning('Cannot get fuel version using fuelclient')
            self.logger.error(e, exc_info=True)

        return fuel_version

    def _get_master_release_fuel_cli(self):
        self.logger.info('use CLI for getting fuel release')

        cmd = 'fuel --fuel-version --json'

        version_info_str, err, code = utils.ssh_node(
            ip=self.conf.fuel_ip,
            command=cmd,
            env_vars=self.cli_creds,
            ssh_opts=self.conf.ssh_opts,
            timeout=self.conf.timeout)
        if code != 0:
            self.logger.warning('NodeManager: cannot get fuel release '
                                'from CLI: {}'.format(err))
            return
        version_info = json.loads(version_info_str)
        release = version_info.get('release')
        if release is None:
            logger.warning('Node manager: cannot get fuel release')
        return release

    def get_slave_nodes_release(self):
        if self.fuel_client is not None:
            return self._get_slaves_release_fuel_client()
        else:
            return self._get_slaves_release_fuel_cli()

    def _get_slaves_release_fuel_client(self):

        try:
            clusters = self.fuel_client.get_request('clusters')
            self.logger.debug('clusters response:%s' % clusters)
        except Exception as e:
            self.logger.warning('Cannot get clusters info using fuelclient')
            self.logger.error(e, exc_info=True)
            return None

        release_map = dict(
            (
                cluster['id'], cluster['fuel_version']
            ) for cluster in clusters
        )

        return release_map

    def _get_slaves_release_fuel_cli(self):
        self.logger.info('use CLI for getting nodes release')

        cmd = 'fuel environment --json'

        clusters_info_str, err, code = utils.ssh_node(
            ip=self.conf.fuel_ip,
            command=cmd,
            env_vars=self.cli_creds,
            ssh_opts=self.conf.ssh_opts,
            timeout=self.conf.timeout)

        if code != 0:
            self.logger.warning(('NodeManager: cannot get '
                                 'clusters info from CLI: %s') % err)
            return None

        clusters_info = json.loads(clusters_info_str)

        release_map = dict(
            (
                cluster['id'], cluster['fuel_version']
            ) for cluster in clusters_info
        )

        return release_map

    def _nodes_init(self):
        release_map = self.get_slave_nodes_release()

        filtered_nodes = self.nodes_filter.filter_nodes(self.nodes_json)

        self._check_filtration_results(filtered_nodes)

        for node_data in filtered_nodes:
            node_id = int(node_data['id'])
            node_roles = node_data.get('roles')

            if not node_roles:
                roles = ['None']
            elif isinstance(node_roles, list):
                roles = node_roles
            else:
                roles = str(node_roles).split(', ')

            keys = "fqdn name mac os_platform status online ip".split()

            cluster_id = \
                node_data['cluster'] if node_data['cluster'] else None

            node_release = release_map.get(cluster_id, 'n/a')
            self.logger.info('node: {0} - release: {1}'.
                             format(node_id, node_release))

            params = {'id': int(node_data['id']),
                      'cluster': cluster_id,
                      'release': node_release,
                      'roles': roles,
                      'conf': self.conf}

            for key in keys:
                params[key] = node_data[key]

            node = Node(**params)
            self.nodes[node.ip] = node

    def _check_filtration_results(self, filtered_nodes):

        all_nodes_num = len(self.nodes_json)
        filtered_nodes_num = len(filtered_nodes)

        if all_nodes_num > 0 and filtered_nodes_num == 0:
            msg = 'No valid nodes were found which could fit ' \
                  'filter parameters.'
            raise exceptions.AllNodesFiltered(msg)

        elif all_nodes_num == filtered_nodes_num:
            self.logger.info('All available nodes passed filtration.')

        elif all_nodes_num > filtered_nodes:
            self.logger.info('Amount of filtered out nodes: {}'.format(
                all_nodes_num - filtered_nodes))

    def _conf_assign_once(self):
        once = Node.conf_once_prefix
        p = Node.conf_match_prefix
        once_p = once + p
        for k in [k for k in self.conf if k.startswith(once)]:
            attr_name = k[len(once_p):]
            assigned = dict((k, None) for k in self.conf[k])
            for ak in assigned:
                for node in self.nodes.values():
                    if hasattr(node, attr_name) and not assigned[ak]:
                        attr = utils.w_list(getattr(node, attr_name))
                        for v in attr:
                            if v == ak:
                                once_conf = self.conf[k][ak]
                                node.apply_conf(once_conf, clean=False)
                                assigned[ak] = node.id
                                break
                    if assigned[ak]:
                        break

    def nodes_reapply_conf(self):
        for node in self.nodes.values():
            node.apply_conf(self.conf)

    @utils.run_with_lock
    def run_commands(self, timeout=15, fake=False, maxthreads=100):
        run_items = []
        for key, node in self.nodes.items():
            run_items.append(utils.RunItem(target=node.exec_cmd,
                                           args={'fake': fake},
                                           key=key))
        result = utils.run_batch(run_items, maxthreads, dict_result=True)
        for key in result:
            self.nodes[key].mapcmds = result[key][0]
            self.nodes[key].mapscr = result[key][1]


class NodeFilter(object):
    """
    Implements node filtering logic
    """

    def __init__(self):
        self.filters = self._get_filters()

    def _get_filters(self):
        config = configuration.get_config()
        return config.filters

    @property
    def check_master(self):
        return self.filters.get('check_master', False)

    def filter_nodes(self, nodes_info):

        filtered_nodes = copy.deepcopy(nodes_info)
        filter_attrs = self._prepare_filter_attrs()

        for filter_attr in filter_attrs:
            filtered_nodes = self._do_filter(filtered_nodes, filter_attr)

        self._online_filter(filtered_nodes)
        return filtered_nodes

    def _prepare_filter_attrs(self):
        filter_attrs = [attr for attr in self.filters.keys()
                        if attr not in ['check_master', 'online']]

        non_empty_filter_attrs = [attr for attr in filter_attrs
                                  if len(self.filters[attr]) > 0]

        return non_empty_filter_attrs

    def _online_filter(self, nodes_info):
        return [node for node in nodes_info if node.get('online')]

    def _do_filter(self, nodes_info, attr):
        # attr from node can be a string or a list
        # so we have to handle it properly
        # TODO: refactor this ugly solution
        def _to_set(data):
            return set([data]) if \
                isinstance(data, string_types) or \
                not isinstance(data, Iterable) else set(data)

        return [node for node in nodes_info
                if len(
                    _to_set(node.get(attr)).intersection(
                        _to_set(self.filters[attr]))
                ) > 0]
