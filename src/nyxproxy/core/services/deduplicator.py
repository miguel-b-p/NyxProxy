import json
import os
import hashlib
import time
import base64
import urllib.parse
from datetime import datetime
from collections import defaultdict
from multiprocessing import Pool, cpu_count

def generate_hash_worker(config):
    key_string = ConfigDeduplicator.get_config_key_string(config)
    return hashlib.md5(key_string.encode('utf-8')).hexdigest()

class ConfigDeduplicator:
    def __init__(self, configs_list, output_dir=None, console=None):
        package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if output_dir is None:
            output_dir = os.path.join(package_dir, 'data', 'unique')
        self.output_dir = output_dir
        self.configs = configs_list
        self.stats = {
            'total_configs': 0,
            'unique_configs': 0,
            'duplicates_removed': 0,
            'protocols': defaultdict(int),
            'duplicate_groups': 0
        }
        self.unique_configs = []
        self.duplicate_groups = []
        self.console = console

    def _prepare_configs(self):
        self.stats['total_configs'] = len(self.configs)
        for config in self.configs:
            protocol = config.get('type', 'unknown')
            self.stats['protocols'][protocol] += 1

    @staticmethod
    def get_config_key_string(config):
        keys_to_hash = [
            'type', 'server', 'port', 'uuid', 'password', 'network',
            'path', 'host', 'tls', 'sni', 'alpn'
        ]
        parts = [f"{key}:{config.get(key, '')}" for key in keys_to_hash]
        return '|'.join(parts)

    def find_duplicates(self):
        self._prepare_configs()

        with Pool(cpu_count()) as pool:
            hashes = list(pool.imap(generate_hash_worker, self.configs, chunksize=100))

        hash_to_configs = defaultdict(list)
        for i, (config, config_hash) in enumerate(zip(self.configs, hashes)):
            config['_hash'] = config_hash
            config['_original_index'] = i
            hash_to_configs[config_hash].append(config)

        for config_hash, configs_group in hash_to_configs.items():
            if len(configs_group) > 1:
                self.duplicate_groups.append(configs_group)
                self.stats['duplicate_groups'] += 1
                self.unique_configs.append(max(configs_group, key=self.config_score))
                self.stats['duplicates_removed'] += len(configs_group) - 1
            else:
                self.unique_configs.append(configs_group[0])

        self.stats['unique_configs'] = len(self.unique_configs)

    @staticmethod
    def config_score(config):
        score = 0
        if config.get('remarks', '').strip():
            score += 10
        score += sum(1 for v in config.values() if v and str(v).strip() and not str(v).startswith('_'))
        score -= config.get('_original_index', 0) * 0.001
        return score

    def clean_config(self, config):
        cleaned = config.copy()
        for key in list(cleaned.keys()):
            if key.startswith('_'):
                del cleaned[key]
        return cleaned

    def reconstruct_config_url(self, config):
        try:
            config_copy = config.copy()
            protocol = config_copy.get('type', '')
            if protocol == 'vmess':
                return self.reconstruct_vmess_url(config_copy)
            elif protocol == 'vless':
                return self.reconstruct_vless_url(config_copy)
            elif protocol == 'trojan':
                return self.reconstruct_trojan_url(config_copy)
            elif protocol == 'shadowsocks':
                return self.reconstruct_shadowsocks_url(config_copy)
            elif protocol == 'ssr':
                return self.reconstruct_ssr_url(config_copy)
            elif protocol == 'tuic':
                return self.reconstruct_tuic_url(config_copy)
            elif protocol == 'hysteria2':
                return self.reconstruct_hysteria2_url(config_copy)
            else:
                return None
        except Exception as e:
            return None

    def reconstruct_vmess_url(self, config):
        try:
            if 'raw_config' in config and isinstance(config['raw_config'], dict):
                raw_config_copy = config['raw_config'].copy()
                if config.get('remarks'):
                    raw_config_copy['ps'] = config['remarks']
                raw_json = json.dumps(raw_config_copy, separators=(',', ':'))
                encoded = base64.b64encode(raw_json.encode('utf-8')).decode('utf-8')
                return f"vmess://{encoded}"
            else:
                vmess_data = {
                    'v': '2',
                    'ps': config.get('remarks', ''),
                    'add': config.get('server', ''),
                    'port': str(config.get('port', 443)),
                    'id': config.get('uuid', ''),
                    'aid': str(config.get('alterId', 0)),
                    'scy': config.get('cipher', 'auto'),
                    'net': config.get('network', 'tcp'),
                    'type': config.get('type_network', ''),
                    'host': config.get('host', ''),
                    'path': config.get('path', ''),
                    'tls': config.get('tls', ''),
                    'sni': config.get('sni', ''),
                    'alpn': config.get('alpn', ''),
                    'fp': config.get('fingerprint', '')
                }
                raw_json = json.dumps(vmess_data, separators=(',', ':'))
                encoded = base64.b64encode(raw_json.encode('utf-8')).decode('utf-8')
                return f"vmess://{encoded}"
        except:
            return None

    def reconstruct_vless_url(self, config):
        try:
            server = config.get('server', '')
            port = config.get('port', 443)
            uuid = config.get('uuid', '')
            remarks = config.get('remarks', '')
            params = {}
            if config.get('flow'): params['flow'] = config['flow']
            if config.get('encryption'): params['encryption'] = config['encryption']
            if config.get('network'): params['type'] = config['network']
            if config.get('tls'): params['security'] = config['tls']
            if config.get('sni'): params['sni'] = config['sni']
            if config.get('path'): params['path'] = config['path']
            if config.get('host'): params['host'] = config['host']
            if config.get('alpn'): params['alpn'] = config['alpn']
            if config.get('fingerprint'): params['fp'] = config['fingerprint']
            if config.get('headerType'): params['headerType'] = config['headerType']
            if config.get('serviceName'): params['serviceName'] = config['serviceName']
            query_string = urllib.parse.urlencode(params) if params else ''
            fragment = urllib.parse.quote(remarks) if remarks else ''
            url = f"vless://{uuid}@{server}:{port}"
            if query_string:
                url += f"?{query_string}"
            if fragment:
                url += f"#{fragment}"
            return url
        except:
            return None

    def reconstruct_trojan_url(self, config):
        try:
            server = config.get('server', '')
            port = config.get('port', 443)
            password = config.get('password', '')
            remarks = config.get('remarks', '')
            params = {}
            if config.get('sni'): params['sni'] = config['sni']
            if config.get('alpn'): params['alpn'] = config['alpn']
            if config.get('fingerprint'): params['fp'] = config['fingerprint']
            if config.get('allowInsecure'): params['allowInsecure'] = '1'
            if config.get('network'): params['type'] = config['network']
            if config.get('path'): params['path'] = config['path']
            if config.get('host'): params['host'] = config['host']
            query_string = urllib.parse.urlencode(params) if params else ''
            fragment = urllib.parse.quote(remarks) if remarks else ''
            url = f"trojan://{password}@{server}:{port}"
            if query_string:
                url += f"?{query_string}"
            if fragment:
                url += f"#{fragment}"
            return url
        except:
            return None

    def reconstruct_shadowsocks_url(self, config):
        try:
            server = config.get('server', '')
            port = config.get('port', 8080)
            method = config.get('method', 'aes-256-gcm')
            password = config.get('password', '')
            remarks = config.get('remarks', '')
            auth_string = f"{method}:{password}"
            encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
            url = f"ss://{encoded_auth}@{server}:{port}"
            if remarks:
                url += f"#{urllib.parse.quote(remarks)}"
            return url
        except:
            return None

    def reconstruct_ssr_url(self, config):
        try:
            server = config.get('server', '')
            port = config.get('port', 8080)
            protocol = config.get('protocol', 'origin')
            method = config.get('method', 'aes-256-cfb')
            obfs = config.get('obfs', 'plain')
            password = config.get('password', '')
            password_b64 = base64.b64encode(password.encode('utf-8')).decode('utf-8')
            main_part = f"{server}:{port}:{protocol}:{method}:{obfs}:{password_b64}"
            params = []
            if config.get('obfs_param'):
                obfsparam_b64 = base64.b64encode(config['obfs_param'].encode('utf-8')).decode('utf-8')
                params.append(f"obfsparam={obfsparam_b64}")
            if config.get('protocol_param'):
                protoparam_b64 = base64.b64encode(config['protocol_param'].encode('utf-8')).decode('utf-8')
                params.append(f"protoparam={protoparam_b64}")
            remarks_b64 = base64.b64encode(config.get('remarks', '').encode('utf-8')).decode('utf-8')
            params.append(f"remarks={remarks_b64}")
            if config.get('group'):
                group_b64 = base64.b64encode(config['group'].encode('utf-8')).decode('utf-8')
                params.append(f"group={group_b64}")
            if params:
                full_string = f"{main_part}/?{'&'.join(params)}"
            else:
                full_string = main_part
            encoded = base64.b64encode(full_string.encode('utf-8')).decode('utf-8')
            return f"ssr://{encoded}"
        except:
            return None

    def reconstruct_tuic_url(self, config):
        try:
            server = config.get('server', '')
            port = config.get('port', 443)
            uuid = config.get('uuid', '')
            password = config.get('password', '')
            remarks = config.get('remarks', '')
            params = {}
            if config.get('version'): params['version'] = config['version']
            if config.get('alpn'): params['alpn'] = config['alpn']
            if config.get('sni'): params['sni'] = config['sni']
            if config.get('allowInsecure'): params['allowInsecure'] = '1'
            if config.get('congestion_control'): params['congestion_control'] = config['congestion_control']
            if config.get('udp_relay_mode'): params['udp_relay_mode'] = config['udp_relay_mode']
            if config.get('reduce_rtt'): params['reduce_rtt'] = '1'
            query_string = urllib.parse.urlencode(params) if params else ''
            fragment = urllib.parse.quote(remarks) if remarks else ''
            auth_part = f"{uuid}:{password}" if password else uuid
            url = f"tuic://{auth_part}@{server}:{port}"
            if query_string:
                url += f"?{query_string}"
            if fragment:
                url += f"#{fragment}"
            return url
        except:
            return None

    def reconstruct_hysteria2_url(self, config):
        try:
            server = config.get('server', '')
            port = config.get('port', 443)
            auth = config.get('auth', '')
            remarks = config.get('remarks', '')
            params = {}
            if config.get('sni'): params['sni'] = config['sni']
            if config.get('insecure'): params['insecure'] = '1'
            if config.get('pinSHA256'): params['pinSHA256'] = config['pinSHA256']
            if config.get('obfs'): params['obfs'] = config['obfs']
            if config.get('obfs_password'): params['obfs-password'] = config['obfs_password']
            if config.get('up'): params['up'] = config['up']
            if config.get('down'): params['down'] = config['alpn']
            query_string = urllib.parse.urlencode(params) if params else ''
            fragment = urllib.parse.quote(remarks) if remarks else ''
            url = f"hysteria2://{auth}@{server}:{port}"
            if query_string:
                url += f"?{query_string}"
            if fragment:
                url += f"#{fragment}"
            return url
        except:
            return None

    def process(self):
        try:
            self.find_duplicates()
            summary_msg = self.print_final_summary()
            # Store summary for later retrieval
            self.summary_message = summary_msg
            return [self.clean_config(c) for c in self.unique_configs]
        except KeyboardInterrupt:
            if self.console:
                self.console.print("[warning]Processo interrompido pelo usuário.[/warning]")
            return None
        except Exception as e:
            if self.console:
                self.console.print(f"[danger]Erro geral no processo de desduplicação: {e}[/danger]")
            return None

    def print_final_summary(self) -> str:
        """Prints or returns the deduplication summary.
        
        Returns:
            The summary message, or empty string if no duplicates removed.
        """
        reduction_rate = (self.stats['duplicates_removed'] / self.stats['total_configs']) * 100 if self.stats['total_configs'] > 0 else 0
        
        if self.stats['duplicates_removed'] > 0:
            message = (
                f"[info]Removed {self.stats['duplicates_removed']:,} duplicate proxies "
                f"({self.stats['unique_configs']:,} unique configs remaining, a {reduction_rate:.1f}% reduction)."
            )
            if self.console:
                self.console.print(message)
            return message
        return ""
