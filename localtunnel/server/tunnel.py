import json
import time
import logging

import eventlet
import eventlet.timeout
import eventlet.semaphore

class Tunnel(object):
    max_pool_size = 3
    domain_part = 3
    backend_port = None
    active_timeout = 5 * 60
    stats = None

    _tunnels = {}

    def __init__(self, name, client, protect=None, domain=None):
        self.name = name
        self.client = client
        if protect:
            user, passwd = protect.split(':')
            self.protect_user = user
            self.protect_passwd = passwd
            self.protect = True
        else:
            self.protect = False
        self.domain = domain
        self.created = time.time()
        self.updated = time.time()
        self.idle = False
        self.proxy_pool = []
        self.pool_semaphore = eventlet.semaphore.Semaphore(0)

    def add_proxy_backend(self, socket):
        pool_size = len(self.proxy_pool)
        if pool_size < Tunnel.max_pool_size:
            self.proxy_pool.append(socket)
            self.pool_semaphore.release()
            self.updated = time.time()
            self.idle = False
        else:
            raise ValueError("backend:\"{0}\" pool is full".format(
                    self.name))

    def pop_proxy_backend(self, timeout=None):
        with eventlet.timeout.Timeout(timeout, False):
            self.pool_semaphore.acquire()
        if not len(self.proxy_pool):
            return
        return self.proxy_pool.pop()

    def destroy(self):
        cls = self.__class__
        if cls.stats:
            duration = time.time() - self.created
            cls.stats.value('tunnel_duration', duration)
            platform = self.client.split(';', 1)[-1].lower()
            cls.stats.count('usage:{0}'.format(platform), 1)
        for backend in self.proxy_pool:
            backend.close()
        if self == cls._tunnels[self.name]:
            cls._tunnels.pop(self.name, None)


    @classmethod
    def create(cls, obj):
        tunnel = cls(**obj)
        cls._tunnels[tunnel.name] = tunnel
        return tunnel

    @classmethod
    def get_by_hostname(cls, hostname):
        try:
            name = hostname.split('.')[-1 * Tunnel.domain_part]
        except IndexError:
            return
        tunnel = cls._tunnels.get(name)
        if not tunnel:
            for n, tunnel in cls._tunnels.iteritems():
                if tunnel.domain and hostname.endswith(tunnel.domain):
                    return tunnel
        else:
            return tunnel

    @classmethod
    def get_by_control_request(cls, request):
        if request['name'] in cls._tunnels:
            tunnel = cls._tunnels[request['name']]
            if tunnel.client != request['client']:
                raise RuntimeError("Tunnel name '{0}' is being used".format(
                        tunnel.name))
            else:
                tunnel.destroy()
        return cls.create(request)

    @classmethod
    def get_by_proxy_request(cls, request):
        if request['name'] in cls._tunnels:
            tunnel = cls._tunnels[request['name']]
            if tunnel.client != request['client']:
                raise RuntimeError("Tunnel name '{0}' is being used".format(
                        tunnel.name))
            return tunnel

    @classmethod
    def schedule_idle_scan(cls):
        def _scan_idle():
            tunnel_count = len(cls._tunnels)
            idle_count = 0
            for name, tunnel in cls._tunnels.iteritems():
                if time.time() - tunnel.updated > cls.active_timeout:
                    tunnel.idle = True
                    idle_count += 1
            if cls.stats:
                cls.stats.value('total_tunnels', tunnel_count)
                cls.stats.value('idle_tunnels', idle_count)
            if idle_count:
                logging.debug("scan: {0} of {1} tunnels are idle".format(
                    idle_count, tunnel_count))

            cls.schedule_idle_scan()
        eventlet.spawn_after(cls.active_timeout, _scan_idle)
