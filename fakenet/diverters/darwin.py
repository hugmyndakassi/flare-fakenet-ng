import logging
import netifaces
import threading
import pcapy
import traceback
import subprocess as sp
import json
from scapy.all import Ether, IP, conf, TCP, UDP, sendp
from expiringdict import ExpiringDict
from diverters import darutils as dutils
from diverters.diverterbase import DiverterBase
from diverters import fnpacket
from diverters.debuglevels import *
from ctypes import CDLL, Structure, sizeof, byref, create_string_buffer
from ctypes import c_ubyte, c_ushort, c_int, c_uint, c_ulong, c_char, c_void_p
from socket import SOCK_STREAM


ADDR_LINK_ANY = 'ff:ff:ff:ff:ff:ff'
LOOPBACK_IP = '127.0.0.1'
MY_IP = '192.0.2.123'
MY_IP_FAKE = '192.0.2.124'
LOOPBACK_IFACE = 'lo0'
DIVERTER_MODE_KEY = r'darwindivertermode'
KEXT_PATH_KEY = r'darwinkextpath'
DIVERTER_MODE_USER = 'user'
DIVERTER_MODE_KERNEL = 'kernel'
DEFAULT_MODE = DIVERTER_MODE_USER


def make_diverter(dconf, lconf, ipaddrs, loglvl=logging.INFO):
    mode = dconf.get(DIVERTER_MODE_KEY, DEFAULT_MODE).lower()
    if mode == DIVERTER_MODE_USER:
        diverter = UsermodeDiverter(dconf, lconf, ipaddrs, loglvl)
    elif mode == DIVERTER_MODE_KERNEL:
        diverter = KextDiverter(dconf, lconf, ipaddrs, loglvl)
    else:
        return None
    return diverter


class DarwinPacketCtx(fnpacket.PacketCtx):
    def __init__(self, lbl, ip_packet):
        super(DarwinPacketCtx, self).__init__(lbl, str(ip_packet))
        self.to_inject = True
        self.ip_packet = ip_packet
        if TCP in ip_packet:
            self.protocol = 'tcp'
        elif UDP in ip_packet:
            self.protocol = 'udp'
        else:
            self.protocol = ''

class DarwinKextPacketCtx(DarwinPacketCtx):
    def __init__(self, meta, lbl, ip_packet):
        super(DarwinKextPacketCtx, self).__init__(lbl, ip_packet)
        self.meta = meta        


class Injector(object):
    '''
    Handle traffic injection to either a loopback interface or a real interface
    '''
    LOOPBACK_BYTE_HEADER = '\x02\x00\x00\x00'
    def __init__(self):
        super(Injector, self).__init__()
        self.iface = None
        self.is_loopback = True
    
    def initialize(self, iface):
        '''
        Initialize the Injector. Also do some quick validation to make sure
        the iface object contains enough information
        @param iface = {
            'iface'      :   <interface name>
            'dlinkdst'  :   required for none loopback: gateway hardware addr.
            'dlinksrc'  :   required for none loopback: iface hardware addr.
        }
        '''
        name = iface.get('iface')
        if name is None:
            return False
        
        self.is_loopback = name == 'lo0'

        if not self.is_loopback:
            dlinksrc = iface.get('dlinksrc')
            dlinkdst = iface.get('dlinkdst')
            if dlinksrc is None or dlinkdst is None:
                return False
        
        self.iface = iface
        return True
    
    def inject(self, bytez):
        '''
        Inject bytes into an interface without any validation
        '''
        if self.is_loopback:
            bytez = self.LOOPBACK_BYTE_HEADER + str(bytez)
        else:
            bytez = Ether(src=self.iface.get('dlinksrc'),
                          dst=self.iface.get('dlinkdst'))/bytez
        sendp(bytez, iface=self.iface.get('iface'), verbose=False)


class InterfaceMonitor(object):
    TIMEOUT = 3
    def __init__(self, ifname, callback):
        self.monitor_thread = None
        self.is_running = False
        self.timeout = self.TIMEOUT
        self.iface = ifname
        self.callback = callback
        self.logger = logging.getLogger('Diverter.IfaceMonitor')

    
    def start(self):
        e = threading.Event()
        e.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_thread,
                                               args=[e])
        self.is_running = True
        self.monitor_thread.start()
        rc = e.wait(self.timeout)
        return rc
    
    def stop(self):
        self.is_running = False
        if self.monitor_thread is None:
            return True
        rc = self.monitor_thread.join(self.timeout)
        return rc
    
    def _monitor_thread(self, e):
        try:
            self.logger.error('Monitoring %s' % self.iface)
            pc = pcapy.open_live(self.iface, 0xffff, 1, 1)
        except:
            err = traceback.format_exc()
            self.logger.error(err)
            self.is_running = False
            return
        e.set()
        while self.is_running:
            _ts, bytez = pc.next()
            self._process(bytez)
        self.logger.error('monitor thread stopping')
        return

    def _process(self, bytez):
        ip_packet = self.ip_packet_from_bytes(bytez)
        if ip_packet is None:
            return False
        
        pkt = DarwinPacketCtx('DarwinPacket', ip_packet)
        self.callback(pkt)
        return
    
    def ip_packet_from_bytes(self, bytez):
        if self.iface.startswith('lo'):
            return self._ip_packet_from_bytes_loopback(bytez)
        return self._ip_packet_from_bytes(bytez)

    def _ip_packet_from_bytes(self, bytez):
        if len(bytez) <= 0:
            return None
        try:
            eframe = Ether(bytez)
            ipkt = eframe[IP]
        except:
            return None
        return ipkt

    def _ip_packet_from_bytes_loopback(self, bytez):
        if len(bytez) <= 0:
            return None
            
        try:
            ip_packet = IP(bytez[4:])
        except:
            err = traceback.format_exc()
            self.logger.error('Failed to process packet: %s' % (err,))
            return None
        return ip_packet


class KextMonitor(object):
    PF_SYSTEM = 32
    SYSPROTO_CONTROL = 2
    AF_SYS_CONTROL = 2
    CTLIOCGINFO = c_ulong(3227799043)
    MYCONTROLNAME = "com.mandiant.FakeNetDiverter"
    MAX_PKT_JSON = 1024
    OPTNEXTPKT = 1
    OPTINJECTPKT = 2
    OPTDROPPKT = 3
    OPTENABLESWALLOW = 4
    OPTDISABLESWALLOW = 5
    LIB_SYSTEM_PATH = "/usr/lib/libSystem.B.dylib"
    KEXT_PATH = "/Users/me/FakeNetDiverter.kext"

    class sockaddr_ctl(Structure):
        _fields_ = [('sc_len', c_ubyte),
                    ('sc_family', c_ubyte),
                    ('ss_sysaddr', c_ushort),
                    ('sc_id', c_uint),
                    ('sc_unit', c_uint),
                    ('sc_reserved', c_uint * 5)]

    class ctl_info(Structure): 
        _fields_ = [('ctl_id', c_uint),
        ('ctl_name', c_char * 96)]
    

    def __init__(self, callback, kextpath=None):
        self.posix = None
        self.callback = callback
        self.kextpath = self.KEXT_PATH if kextpath is None else kextpath
        self.timeout = 3
    
    def __del__(self):
        self.__unload_kext()

    def initialize(self):
        self.posix = self.__initialize_posix_wrapper()
        if self.posix is None:
            self.logger.error('Failed to initialize POSIX wrapper')
            return False

        if not self.__load_kext():
            return False

        return True
    
    def start(self):
        self.is_running = True
        self.socket = self.__initialize_socket()

        if self.socket is None:
            return False
        
        e = threading.Event()
        e.clear()
        self.monitor_thread = threading.Thread(target=self._monitor_thread,
                                               args=[e])
        self.monitor_thread.start()
        rc = e.wait(self.timeout)
        return rc
    
    def stop(self):
        self.is_running = False
        if self.monitor_thread is None:
            return True
        rc = self.monitor_thread.join(self.timeout)
        self.posix.close(self.socket)
        self.socket = None
        self.posix = None
        self.__unload_kext()
        return rc

    # internal
    def __initialize_posix_wrapper(self):
        posix = CDLL(self.LIB_SYSTEM_PATH, use_errno=True)
        posix.getsockopt.argtypes = [c_int, c_int, c_int, c_void_p, c_void_p]
        posix.setsockopt.argtypes = [c_int, c_int, c_int, c_void_p, c_uint]
        return posix

    def __initialize_socket(self):
        posix = self.posix
        if posix is None:
            return None
        socket = posix.socket(
            self.PF_SYSTEM, SOCK_STREAM, self.SYSPROTO_CONTROL)

        addr = self.sockaddr_ctl()
        addr.sc_len = (c_ubyte)(sizeof(self.sockaddr_ctl))
        addr.sc_family = (c_ubyte)(self.PF_SYSTEM)
        addr.ss_sysaddr = (c_ushort)(self.AF_SYS_CONTROL)

        info = self.ctl_info()
        info.ctl_name = self.MYCONTROLNAME

        rc = posix.ioctl(socket, self.CTLIOCGINFO, byref(info))        

        addr.sc_id = (c_uint)(info.ctl_id)
        addr.sc_unit = (c_uint)(0)
        posix.connect(socket, byref(addr), sizeof(addr))
        return socket
    
    def __load_kext(self):
        try:
            sp.call("kextutil %s" % (self.kextpath,), shell=True)
        except:
            return False
        return True

    def __unload_kext(self):
        if self.socket is not None and self.posix is not None:
            self.posix.close(self.socket)
            self.posix = None
            self.socket = None

        count = 2
        while count > 0:
            try:
                self.logger.error("Unloading kext...")
                x = sp.call("kextunload %s" % (self.kextpath,), shell=True)
            except:
                return False
            sleep(1)
            count -= 1
        return True
    

    def _monitor_thread(self, event):
        event.set()
        self.posix.setsockopt(
            self.socket, self.SYSPROTO_CONTROL, self.OPTENABLESWALLOW, 0, 0)

        while self.is_running:
            pktSize = c_uint(self.MAX_PKT_JSON)
            pkt = create_string_buffer("\x00" * self.MAX_PKT_JSON)
            self.posix.getsockopt(self.socket,
                                  self.SYSPROTO_CONTROL,
                                  self.OPTNEXTPKT, pkt, byref(pktSize))

            try:
                if len(pkt.value) > 0:
                    pktjson = json.loads(pkt.value)
                    newpkt = self.__process(pktjson)
                    if newpkt is None:
                        pkt = byref(c_uint(int(pktjson.get('id'))))
                        pktSize = c_uint(4)
                        self.posix.setsockopt(self.socket,
                                              self.SYSPROTO_CONTROL,
                                              self.OPTDROPPKT, pkt, pktSize)
                    newjson = json.dumps(newpkt)
                    newjson += '\0x00'
                    newpkt = create_string_buffer(newjson)

                    pktSize = c_uint(len(newpkt))

                    self.posix.setsockopt(self.socket,
                                          self.SYSPROTO_CONTROL,
                                          self.OPTINJECTPKT, newpkt, pktSize)
            except:
                traceback.print_exc()
        self.posix.setsockopt(self.socket,
                              self.SYSPROTO_CONTROL,
                              self.OPTDISABLESWALLOW, 0, 0)
        return

    def __process(self, pkt):
        ip_packet = self.ip_packet_from_json(pkt)
        if ip_packet is None:
            return None

        # Process the packet through the callbacks. pctx is updated as it
        # traverse through the callback stack
        pctx = DarwinKextPacketCtx(pkt, 'DarwinKextPacket', ip_packet)
        self.callback(pctx)
        if not pctx.mangled:
            newpkt = {'id': pctx.meta.get('id'), 'changed': False}
        else:
            newpkt = self.json_from_pctx(pctx)
        return newpkt
    
    def ip_packet_from_json(self, js):
        proto = js.get('proto', None)
        sport = js.get('srcport')
        dport = js.get('dstport')
        src = js.get('srcaddr')
        dst = js.get('dstaddr')
        
        if proto is None or sport is None or dport is None:
            return None
        
        if  src is None or dst is None:
            return None
        
        if proto == 'tcp':
            tport = TCP(sport=sport, dport=dport)
        elif proto == 'udp':
            tport = UDP(sport=sport, dport=dport)
        else:
            tport is None        
        if tport is None:
            return None
        
        ip_packet = IP(src=src, dst=dst)/tport
        return ip_packet
    
    def json_from_pctx(self, pctx):
        return {
            u'id': pctx.meta.get('id'),
            u'direction': pctx.meta.get('direction'),
            u'proto': pctx.protocol,
            u'srcaddr': pctx.src_ip,
            u'srcport': pctx.sport,
            u'dstaddr': pctx.dst_ip,
            u'dstport': pctx.dport,
            u'ip_ver': pctx.meta.get('ip_ver'),
            u'changed': pctx.mangled
        }
    
    def drop(self, pkt):
        pkt = byref(c_uint(int(pkt.get('id', -1))))
        pktSize = c_uint(4)
        self.posix.setsockopt(self.socket, self.SYSPROTO_CONTROL,
                              self.OPTDROPPKT, pkt, pktSize)
        return True

class DarwinDiverter(DiverterBase):
    def __init__(self, diverter_config, listeners_config, ip_addrs,
                 logging_level=logging.INFO):
        super(DarwinDiverter, self).__init__(diverter_config, listeners_config,
                                             ip_addrs, logging_level)
        
        self.gw = None
        self.iface = None
            
    def __del__(self):
        self.stopCallback()
    
    def initialize(self):
        self.gw = dutils.get_gateway_info()
        if self.gw is None:
            raise NameError("Failed to get gateway")

        self.iface = dutils.get_iface_info(self.gw.get('iface'))
        if self.iface is None:
            raise NameError("Failed to get public interface")
        
        return
    

    #--------------------------------------------------------------
    # implements various DarwinUtilsMixin methods
    #--------------------------------------------------------------

    def check_active_ethernet_adapters(self):
        return len(netifaces.interfaces()) > 0
    
    def check_ipaddresses(self):
        return True
        
    def check_dns_servers(self):
        return True

    def check_gateways(self):
        return len(netifaces.interfaces()) > 0


class UsermodeDiverter(DarwinDiverter):
    LOOPBACK_IFACE = 'lo0'
    def __init__(self, diverter_config, listeners_config, ip_addrs,
                 logging_level=logging.INFO):
        super(UsermodeDiverter, self).__init__(diverter_config, listeners_config,
                                       ip_addrs, logging_level)
        
        self.loopback_ip = '192.0.2.123'
        self.loopback_ip_fake = '192.0.2.124'
        self.devnull = open('/dev/null', 'rw+')

        self.configs = dict()
        self.is_running = False
        self.iface_monitor = None
        self.loopback_monitor = None
        self.inject_cache = ExpiringDict(max_age_seconds=10, max_len=0xfff)
        self.initialize()        

        # hide scappy noisy logs
        logging.getLogger('scapy.runtime').setLevel(logging.ERROR)
    
    def initialize(self):
        super(UsermodeDiverter, self).initialize()
        
        # initialize a loopback injector
        self.loopback_injector = Injector()
        if not self.loopback_injector.initialize({'iface': 'lo0'}):
            raise NameError("Failed to initialize loopback injector")
        
        # initialize main injector
        self.injector = Injector()
        iface = {
            'iface': self.iface.get('iface'),
            'dlinksrc': self.iface.get('addr.dlink'),
            'dlinkdst': self.gw.get('addr.dlink')
        }
        if not self.injector.initialize(iface):
            raise NameError("Failed to initialize injector")

        return True
    
    def startCallback(self):
        self.iface_monitor = InterfaceMonitor(self.iface.get('iface'),
                                              self.handle_packet_external)
        self.iface_monitor.start()

        self.loopback_monitor = InterfaceMonitor(self.LOOPBACK_IFACE,
                                                 self.handle_packet_internal)
        self.loopback_monitor.start()

        if not self._save_config():
            self.logger.error('Failed to save config')
            return False
        
        if not self._change_config():
            self.logger.error('Failed to change config')
            return False
        
        self.is_running = True
        self.logger.info('%s is running' % (self.__class__.__name__))
        return True
    
    def stopCallback(self):
        self.is_running = False
        if self.iface_monitor is not None:
            self.iface_monitor.stop()
        if self.loopback_monitor is not None:
            self.loopback_monitor.stop()
        self._restore_config()
        return True
        
    #--------------------------------------------------------------
    # main packet handler callback
    #--------------------------------------------------------------
    def handle_packet_external(self, pctx):
        if self._is_my_ip_public(pctx.ip_packet.src):
            return
        
        if not self._is_in_inject_cache(pctx):
            return

        cb3 = []
        cb4 = [self._darwin_fix_ip_external]
        ipkt = pctx.ip_packet
        self.handle_pkt(pctx, cb3, cb4)
        self.handle_inject(pctx)
        return

    def handle_packet_internal(self, pctx):
        '''
        Main callback to handle a packet
        @param pctx: DarwinPacketCtx object created for each packet
        @return True on success False on error
        @NOTE: pctx gets updated as it traverse through this callback
               Check pctx.mangled flag to see if the packet has been
               mangled
        '''
        if not self._is_my_ip_loopback(pctx.ip_packet.src):
            return

        cb3 = [
            self.check_log_icmp,
        ]
        cb4 = [
            self._darwin_fix_ip_internal,
        ]
        self.handle_pkt(pctx, cb3, cb4)
        self.handle_inject(pctx)
        return

    def update_inject_cache(self, pctx):    
        endpoint = fnpacket.PacketCtx.gen_endpoint_key(
            pctx.protocol, pctx.src_ip, pctx.sport)
        self.inject_cache[endpoint] = True
        return True

    def select_injector(self, ip):
        if ip == LOOPBACK_IP:
            return self.loopback_injector
        
        if ip == self.loopback_ip or ip == self.loopback_ip_fake:
            return self.loopback_injector
        
        return self.injector

    def handle_inject(self, pctx):
        if not pctx.to_inject:
            return False

        bytez = self.make_bytez(pctx)
        if bytez is None:
            self.logger.error('Failed to make bytez from pkt_ctx')
            return False
        
        self.update_inject_cache(pctx)

        injector = self.select_injector(pctx.dst_ip)    
        injector.inject(bytez)
        return True
    
    def make_bytez(self, pctx):
        ipkt = pctx.ip_packet
        if pctx.protocol == 'tcp':
            otport = ipkt[TCP]
            pload = TCP(
                sport=pctx.sport, dport=pctx.dport,
                seq=otport.seq, ack=otport.ack, dataofs=otport.dataofs,
                window=otport.window, flags=otport.flags, options=otport.options
            )/otport.payload
        elif pctx.protocol == 'udp':
            otport = ipkt[UDP]
            pload = UDP(sport=pctx.sport, dport=pctx.dport)/otport.payload
        else:
            pload = ipkt.payload
        
        bytez = IP(src=pctx.src_ip, dst=pctx.dst_ip)/pload
        return bytez
        
        

    #--------------------------------------------------------------
    # implements various DarwinUtilsMixin methods
    #--------------------------------------------------------------
    def getNewDestination(self, ip):
        if ip == self.loopback_ip_fake:
            return self.loopback_ip
        return self.loopback_ip_fake
    
    def getLoopbackDestination(self):
        return self.loopback_ip

    def check_should_ignore(self, pkt, pid, comm):
        if super(UsermodeDiverter, self).check_should_ignore(pkt, pid, comm):
            return True
        
        if pkt.src_ip == self.loopback_ip:
            return False
        if pkt.src_ip == self.loopback_ip_fake:
            return False
        
        pkt.to_inject = False
        return True

    def _darwin_fix_ip_external(self, crit, pkt, pid, comm):
        newdst = self.getLoopbackDestination()
        pkt.dst_ip = newdst
        pkt.to_inject = True
        return

    def _darwin_fix_ip_internal(self, crit, pkt, pid, comm):
        '''
        Check if we should redirect this packet to local listener
        '''

        if self.check_should_ignore(pkt, pid, comm):
            pkt.src_ip = self.iface.get('addr.inet')[0]
            return True
        
        # always assume that we are in single host mode
        # hacky: swap src/dst before changing
        newdst = self.getNewDestination(pkt.src_ip)
        pkt.src_ip, pkt.dst_ip = pkt.dst_ip, pkt.src_ip
        pkt.dst_ip = newdst
        return             

        
    #--------------------------------------------------------------
    # implements various DirverterPerOSDelegate() abstract methods
    #--------------------------------------------------------------

    def get_pid_comm(self, pkt):
        '''
        Given a packet, return pid and command/process name that generates the
        packet. 
        @param pkt: DarwinPacketCtx
        @return None, None if errors
        '''
        return self._get_pid_comm(pkt)
    
    

    # -----------------------------------------------------------------
    # Internal methods, do not call!
    # -----------------------------------------------------------------
    def _change_config(self):
        '''
        Apply the following network configuration changese:
        - Add an IP alias to the loopback interface.
        - Change the default gateway to the newly alias IP.
        - Enable forwarding if it is currently disabled.
        @return True on sucess, False on failure.
        '''
        if len(self.configs) <= 0:
            if not self._save_config():
                self.logger.error('Save config failed')
                return False
        if not self._add_loopback_alias():
            self.logger.error('Failed to add loopback alias')
            return False
        if not self._change_default_route():
            self.logger.error('Failed to change default route')
            return False
        return True


    def _save_config(self):
        '''
        Save the following network configuration:
        - net.inet.ip.forwarding
        - Current default gateway
        @return True on sucess, False on failure.
        '''
        configs = dict()
        try:
            ifs = sp.check_output('sysctl net.inet.ip.forwarding',
                                  shell=True, stderr=self.devnull)
            _,v = ifs.strip().split(':', 2)
            v = int(v, 10)
        except:
            self.logger.error('Save config failed')
            return False
        configs['net.forwarding'] = v

        try:
            iface, ipaddr, gw = conf.route.route('0.0.0.0')
        except:
            return False
        configs['net.iface'] = iface
        configs['net.ipaddr'] = ipaddr
        configs['net.gateway'] = gw
        self.configs = configs
        return True

    def _add_loopback_alias(self):
        '''Try to execute all commands. Only return success if all commands are
        executed successfully
        '''
        cmds = [
            'ifconfig lo0 alias %s' % (self.loopback_ip,),
            'ifconfig lo0 alias %s' % (self.loopback_ip_fake,),
        ]
        for cmd in cmds:
            if not self._quiet_call(cmd):
                return False
        return True

    def _change_default_route(self):
        '''
        Try to change the default route. If that fails, add a default route
        to the specified IP address
        '''
        cmds = [
            'route -n change default %s' % (self.loopback_ip,),
            'route -n add default %s' % (self.loopback_ip,),
        ]
        for cmd in cmds:
            if self._quiet_call(cmd):
                return True
        return False


    def _restore_config(self):
        '''
        Restore the following network settings. This should always
        return True
        - Default route
        - Remove loopback IP aliases
        @return True on sucess, False on failure.
        '''
        if len(self.configs) == 0:
            return True
        self._fix_default_route()
        self._remove_loopback_alias()
        return True

    def _remove_loopback_alias(self):
        cmds = [
            'ifconfig lo0 -alias %s' % (self.loopback_ip,),
            'ifconfig lo0 -alias %s' % (self.loopback_ip_fake,)
        ]
        for cmd in cmds:
            if not self._quiet_call(cmd):
                return False
        return True

    def _fix_default_route(self):
        gw = self.configs.get('net.gateway', None)
        if gw is None:
            return self._quiet_call('route -n delete default')
        return self._quiet_call('route -n change default %s'% (gw,))


    def _quiet_call(self, cmd):
        '''
        Simple wrapper to execute shell command quietly
        @attention: Is shell=True a security concern?
        '''
        try:
            sp.check_call(cmd,
                          stdout=self.devnull,
                          stderr=sp.STDOUT,
                          shell=True)
        except:
            self.logger.error('Failed to run: %s' % (cmd,))
            traceback.print_exc()
            stk = traceback.format_exc()
            self.logger.debug(">>> Stack:\n%s" % (stk,))
            return False
        return True\
    
    def _get_pid_comm(self, ipkt):
        if not ipkt.protocol == 'tcp' and not ipkt.protocol == 'udp':
            return None, None

        now = datetime.now()
        protospec = "-i%s%s@%s" % (
            ipkt.ip_packet.version, ipkt.protocol, ipkt.dst_ip)
        
        if ipkt.dport:
            protospec = "%s:%s" % (protospec, ipkt.dport)
        cmd = [
            'lsof', '-wnPF', 'cLn',
            protospec
        ]
        with open('lsof.txt', 'a+') as ofile:
            ofile.write("%s\n" % (protospec,))

        try:
            result = sp.check_output(cmd, stderr=None).strip()
        except:
            result = None
        if result is None:
            return None, None
        
        lines = result.split('\n')
        # print 'YYY elapsed 0', datetime.now() - now
        for record in self._generate_records(lines):
            _result = self._parse_record(record)
            if _result is None:
                continue
            if self._is_my_packet(_result):
                # print 'XXX elapsed:', datetime.now() - now
                return _result.get('pid'), _result.get('comm')
        
        return None, None
    
    def _generate_records(self, lines):
        n = len(lines)
        maxlen = (n // 5) * 5
        lines = lines[:maxlen]
        for i in xrange(0, len(lines), 5):
            try:
                record = lines[i:i+5]
                pid = record[0][1:]
                comm = record[1][1:]
                uname = record[2][1:]
                name = record[4][1:]
                yield {'pid': pid, 'comm': comm, 'name': name, 'uname': uname}
            except IndexError:
                print record[2]
                yield {}
    
    def _parse_record(self, record):
        name = record.get('name')
        if name is None:
            return None
        
        try:
                src_endpoint, dst_endpoint = name.split('->')
                src, sport = src_endpoint.split(':')
                dst, dport = dst_endpoint.split(':')
        except:
            return None
        
        record.update({'src': src, 'dst': dst, 'sport': sport, 'dport': dport})
        record['pid'] = int(record.get('pid'))
        return record
    
    def _is_my_packet(self, record):
        src, dst = record.get('src'), record.get('dst')
        if src == self.loopback_ip or src == self.loopback_ip_fake:
            return True
        
        if dst == self.loopback_ip or dst == self.loopback_ip_fake:
            return True
        
        return False
    
    def _is_my_ip_loopback(self, ip):
        if ip == self.loopback_ip or ip == self.loopback_ip_fake:
            return True
        return False
    
    def _is_my_ip_public(self, ip):
        try:
            rc = ip == self.iface.get('addr.inet')[0]
        except:
            rc = False
        return rc
    
    def _is_in_inject_cache(self, pctx):
        endpoint = fnpacket.PacketCtx.gen_endpoint_key(
            pctx.protocol, pctx.dst_ip, pctx.dport)
        return endpoint in self.inject_cache


class KextDiverter(DarwinDiverter):
    def __init__(self, diverter_config, listeners_config, ip_addrs, log_level):
        super(KextDiverter, self).__init__(diverter_config, listeners_config,
                                           ip_addrs, log_level)
        self.kextpath = diverter_config.get(KEXT_PATH_KEY, None)
        self.monitor = None
        self.initialize()
    
    def initialize(self):
        self.monitor = KextMonitor(self.handle_packet, self.kextpath)
        if not self.monitor.initialize():
            self.monitor = None
            raise NameError("Failed to initialize monitor")
        return True
    
    def handle_packet(self, pctx):
        direction = pctx.meta.get('direction')
        cb3 = [
            self.check_log_icmp   
        ]

        if direction == 'out':
            cb4 = [
                self.maybe_redir_ip,
                self.maybe_redir_port,
            ]
        else:
            cb4 = [
                self.maybe_fixup_sport,
                self.maybe_fixup_srcip,
            ]
        self.handle_pkt(pctx, cb3, cb4)
        return 
    
    def get_pid_comm(self, pkt):
        return pkt.meta.get('pid', ''), pkt.meta.get('procname', '')
    
    def startCallback(self):
        self.monitor.start()
        return True
    
    def stopCallback(self):
        self.monitor.stop()
        return
    
    def getNewDestinationIp(self, ip):
        return LOOPBACK_IP