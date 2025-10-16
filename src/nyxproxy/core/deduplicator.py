import json
import os
import hashlib
import logging
import time
import base64
import urllib.parse
from datetime import datetime
from collections import defaultdict
from multiprocessing import Pool, cpu_count
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def generate_hash_worker(config):
    key_string = ConfigDeduplicator.get_config_key_string(config)
    return hashlib.md5(key_string.encode('utf-8')).hexdigest()

class ConfigDeduplicator:
    def __init__(self, configs_list, output_dir=None):
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

    def _prepare_configs(self):
        """Prepara as configurações iniciais e preenche as estatísticas."""
        self.stats['total_configs'] = len(self.configs)
        for config in self.configs:
            protocol = config.get('type', 'unknown')
            self.stats['protocols'][protocol] += 1

    @staticmethod
    def get_config_key_string(config):
        """Gera uma string canônica para uma configuração para hashing."""
        # Chaves que definem a unicidade de uma configuração
        keys_to_hash = [
            'type', 'server', 'port', 'uuid', 'password', 'network',
            'path', 'host', 'tls', 'sni', 'alpn'
        ]
        parts = [f"{key}:{config.get(key, '')}" for key in keys_to_hash]
        return '|'.join(parts)

    def find_duplicates(self):
        logging.info("Iniciando análise e detecção de duplicatas...")
        self._prepare_configs()
        start_time = time.time()

        # Fase 1: Geração de Hashes em Paralelo
        logging.info("Fase 1: Gerando hashes e agrupando configurações...")
        num_workers = cpu_count()
        logging.info(f"Utilizando {num_workers} workers para processamento paralelo.")
        with Pool(num_workers) as pool:
            hashes = list(tqdm(pool.imap(generate_hash_worker, self.configs, chunksize=100),
                                total=len(self.configs), desc="Gerando Hashes", unit="cfg"))

        hash_to_configs = defaultdict(list)
        for i, (config, config_hash) in enumerate(zip(self.configs, hashes)):
            config['_hash'] = config_hash # Adiciona o hash ao dict original
            config['_original_index'] = i
            hash_to_configs[config_hash].append(config)

        hash_time = time.time() - start_time
        logging.info(f"Geração de hash concluída em {hash_time:.2f} segundos.")
        logging.info(f"Encontrados {len(hash_to_configs)} grupos de configurações únicas (baseado em hash).")

        # Fase 2: Processamento dos grupos de duplicatas
        logging.info("Fase 2: Processando grupos e selecionando a melhor configuração...")
        duplicate_start = time.time()

        for config_hash, configs_group in tqdm(hash_to_configs.items(), desc="Processing groups", unit="group"):
            if len(configs_group) > 1:
                self.duplicate_groups.append(configs_group)
                self.stats['duplicate_groups'] += 1
                self.unique_configs.append(max(configs_group, key=self.config_score))
                self.stats['duplicates_removed'] += len(configs_group) - 1
            else:
                self.unique_configs.append(configs_group[0])

        self.stats['unique_configs'] = len(self.unique_configs)
        duplicate_time = time.time() - duplicate_start
        total_time = time.time() - start_time
        logging.info(f"Processamento de duplicatas concluído em {duplicate_time:.2f} segundos.")
        logging.info(f"Tempo total da análise: {total_time:.2f} segundos.")
        efficiency = (self.stats['duplicates_removed'] / self.stats['total_configs']) * 100 if self.stats['total_configs'] > 0 else 0
        logging.info(f"Análise de duplicatas concluída:")
        logging.info(f"   Total: {self.stats['total_configs']:,} | Únicas: {self.stats['unique_configs']:,} | Removidas: {self.stats['duplicates_removed']:,}")
        logging.info(f"   Otimização: {efficiency:.1f}% | Grupos de duplicatas: {self.stats['duplicate_groups']:,}")
        if total_time > 0:
            logging.info(f"   Velocidade de processamento: {self.stats['total_configs']/total_time:.0f} configs/segundo")

    @staticmethod
    def config_score(config):
        """Calcula uma pontuação para uma configuração para determinar a 'melhor' em um grupo de duplicatas."""
        score = 0
        # Prefere configurações com 'remarks' preenchido
        if config.get('remarks', '').strip():
            score += 10
        # Prefere configurações com mais campos preenchidos
        score += sum(1 for v in config.values() if v and str(v).strip() and not str(v).startswith('_'))
        # Desempate: prefere a que apareceu primeiro no arquivo original (índice menor)
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
            # logging.debug(f"Falha ao reconstruir URL para {config.get('server')}: {e}")
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
            url = f"vless://{uuid} @{server}:{port}"
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
            url = f"trojan://{password} @{server}:{port}"
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
            url = f"ss://{encoded_auth} @{server}:{port}"
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
            url = f"tuic://{auth_part} @{server}:{port}"
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
            url = f"hysteria2://{auth} @{server}:{port}"
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
            self.print_final_summary()
            # Retorna a lista de configurações únicas e limpas
            return [self.clean_config(c) for c in self.unique_configs]
        except KeyboardInterrupt:
            logging.warning("Processo interrompido pelo usuário.")
            return None
        except Exception as e:
            logging.critical(f"Erro geral no processo de desduplicação: {e}", exc_info=True)
            return None
    def print_final_summary(self):
        title = "DUPLICATE REMOVAL - FINAL SUMMARY"
        logging.info("\n" + "=" * 60)
        logging.info(title.center(60))
        logging.info("=" * 60)
        reduction_rate = (self.stats['duplicates_removed'] / self.stats['total_configs']) * 100 if self.stats['total_configs'] > 0 else 0
        logging.info(f"Configurações originais: {self.stats['total_configs']:,}")
        logging.info(f"Configurações únicas: {self.stats['unique_configs']:,}")
        logging.info(f"Duplicatas removidas: {self.stats['duplicates_removed']:,}")
        logging.info(f"Grupos de duplicatas: {self.stats['duplicate_groups']:,}")
        logging.info(f"Taxa de redução: {reduction_rate:.1f}%")
        logging.info("Detalhamento por protocolo (original):")
        for protocol, count in self.stats['protocols'].items():
            logging.info(f"   - {protocol}: {count:,} configs")
        logging.info(f"Diretório de saída: {self.output_dir}")
        logging.info("=" * 60)

def main():
    title = "Remove duplicate configurations"
    print(title)
    print("=" * len(title))
    deduplicator = ConfigDeduplicator()
    if deduplicator.process():
        logging.info("Processo de desduplicação concluído com sucesso!")
    else:
        logging.error("Processo de desduplicação encontrou um erro ou foi interrompido.")

if __name__ == "__main__":
    main()