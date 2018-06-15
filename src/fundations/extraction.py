# -*- coding: utf-8 -*-
"""extractor for PCAP files

`jspcap.tools.extraction` contains `Extractor` only, which
synthesises file I/O and protocol analysis, coordinates
information exchange in all network layers, extracst
parametres from a PCAP file.

"""
import collections
import copy
import datetime
import io
import multiprocessing
import os
import pathlib
import random
import re
import textwrap
import time

###############################################################################
# from jsformat import PLIST, JSON, Tree, JavaScript, XML
###############################################################################

from jspcap.protocols.pcap.frame import Frame
from jspcap.protocols.pcap.header import Header
from jspcap.utilities.exceptions import CallableError, FormatError, \
        FileNotFound, UnsupportedCall, IterableError
from jspcap.utilities.infoclass import Info

###############################################################################
# from jspcap.reassembly.ipv4 import IPv4_Reassembly
# from jspcap.reassembly.ipv6 import IPv6_Reassembly
# from jspcap.reassembly.tcp import TCP_Reassembly
###############################################################################


__all__ = ['Extractor']


# CPU number
if os.name == 'posix' and 'SC_NPROCESSORS_CONF' in os.sysconf_names:
    CPU_CNT = os.sysconf('SC_NPROCESSORS_CONF')
elif 'sched_getaffinity' in os.__all__:
    CPU_CNT = len(os.sched_getaffinity(0))
else:
    CPU_CNT = os.cpu_count() or 1


class Extractor:
    """Extractor for PCAP files.

    Properties:
        * info -- VerionInfo, version of input PCAP file
        * length -- int, frame number (of current extracted frame or all)
        * format -- str, format of output file
        * input -- str, name of input PCAP file
        * output -- str, name of output file
        * header -- Info, global header
        * frames -- tuple<Info>, extracted frames
        * protocol -- ProtoChain, protocol chain of current/last frame
        * reassembly -- Info, frame record for reassembly
            |--> tcp -- tuple<TCP_Reassembly>, TCP payload fragment reassembly
            |--> ipv4 -- tuple<IPv4_Reassembly>, IPv4 frame fragment reassembly
            |--> ipv6 -- tuple<IPv6_Reassembly>, IPv6 frame fragment reassembly

    Methods:
        * make_name -- formatting input & output file name
        * record_header -- extract global header
        * record_frames -- extract frames

    Attributes:
        * _flag_a -- bool, if run automatically to the end
        * _ifnm -- str, input file name (aka _ifile.name)
        * _ofnm -- str, output file name (aka _ofile.name)
        * _type -- str, output file kind (aka _ofile.kind)
        * _fext -- str, output file extension

        * _ifile -- FileIO, input file object
        * _ofile -- object, temperory output writer

        * _frnum -- int, frame number
        * _frame -- list, each item contains `Info` of a record/package
            |--> gbhdr -- Info object, global header
            |--> frame 1 -- Info object, record/package header
            |       |--> Info object, first (link layer) header
            |       |--> Info object, next protocol header
            |       |--> ......
            |       |--> Info object, optional protocol trailer
            |--> frame 2 -- Info object, record/package header
            |       |--> ......
        * _reasm -- list, frame reassembly instance
            |--> IPv4 -- IPv4_Reassembly, reassembly instance
            |--> IPv6 -- IPv6_Reassembly, reassembly instance
            |--> TCP -- TCP_Reassembly, reassembly instance

        * _gbhdr -- Info object, the global header
        * _dlink -- str, data link layer protocol of input file
        * _vinfo -- VersionInfo, version of input file
        * _proto -- str, protocol chain of current frame

        * _ip -- bool, flag if perform IPv4 & IPv6 reassembly
        * _ipv4 -- bool, flag if perform IPv4 reassembly
        * _ipv6 -- bool, flag if perform IPv6 reassembly
        * _tcp -- bool, flag if perform TCP payload reassembly

    Utilities:
        * _read_frame -- read frames
        * _tcp_reassembly -- store data for TCP reassembly
        * _ipv4_reassembly -- store data for IPv4 reassembly
        * _ipv6_reassembly -- store data for IPv6 reassembly

    """

    ##########################################################################
    # Properties.
    ##########################################################################

    @property
    def info(self):
        return self._vinfo

    @property
    def length(self):
        return self._frnum - 1

    @property
    def format(self):
        if self._flag_q:
            raise UnsupportedCall("'Extractor(nofile=True)' object has no attribute 'format'")
        return self._type

    @property
    def input(self):
        return self._ifnm

    @property
    def output(self):
        if self._flag_q:
            raise UnsupportedCall("'Extractor(nofile=True)' object has no attribute 'format'")
        return self._ofnm

    @property
    def header(self):
        return self._gbhdr

    @property
    def protocol(self):
        return self._proto

    @property
    def frame(self):
        if self._flag_d:
            return tuple(self._frame)
        raise UnsupportedCall("'Extractor(store=False)' object has no attribute 'frame'")

    @property
    def reassembly(self):
        data = Info(
            ipv4 = tuple(self._reasm[0].datagram) if self._ipv4 else None,
            ipv6 = tuple(self._reasm[1].datagram) if self._ipv6 else None,
            tcp = tuple(self._reasm[2].datagram) if self._tcp else None,
        )
        return data

    ##########################################################################
    # Methods.
    ##########################################################################

    @classmethod
    def make_name(cls, fin, fout, fmt, extension, *, files=False, nofile=False):
        if fin is None:
            ifnm = 'in.pcap'
        else:
            if extension:
                ifnm = fin if os.path.splitext(fin)[1] == '.pcap' else f'{fin}.pcap'
            else:
                ifnm = fin

        if not os.path.isfile(ifnm):
            raise FileNotFound(f"[Errno 2] No such file or directory: '{ifnm}'")

        if nofile:
            ofnm = None
            ext = None
        else:
            fmt_none = (fmt is None)

            if fmt == 'html':   ext = 'js'
            elif fmt == 'tree': ext = 'txt'
            else:               ext = fmt

            if fout is None:
                if fmt_none:
                    raise FormatError('Output format unspecified.')
                elif files:
                    ofnm = 'out'
                    pathlib.Path(ofnm).mkdir(parents=True, exist_ok=True)
                else:
                    ofnm = f'out.{ext}'
            else:
                name, fext = os.path.splitext(fout)
                if fext:
                    files = False
                    ofnm = fout
                    fmt = fmt or fext[1:] or None
                    if fmt is None:
                        raise FormatError('Output format unspecified.')
                elif fmt_none:
                    raise FormatError('Output format unspecified.')
                elif files:
                    ofnm = fout
                    pathlib.Path(ofnm).mkdir(parents=True, exist_ok=True)
                elif extension: ofnm = f'{fout}.{ext}'
                else:           ofnm = fout

        return ifnm, ofnm, fmt, ext, files

    # def record_header(self):
    #     """Read global header.

    #     - Extract global header.
    #     - Make Info object out of header properties.
    #     - Append Info.
    #     - Write plist file.

    #     """
    #     self._gbhdr = Header(self._ifile)
    #     self._dlink = self._gbhdr.protocol
    #     self._vinfo = self._gbhdr.info
    #     if not self._flag_q:
    #         if self._flag_f:
    #             ofile = self._ofile(f'{self._ofnm}/Global Header.{self._fext}')
    #             ofile(self._gbhdr.info, name='Global Header')
    #             self._type = ofile.kind
    #         else:
    #             self._ofile(self._gbhdr.info, name='Global Header')
    #             self._type = self._ofile.kind

    # def record_frames(self):
    #     if self._flag_a:
    #         while True:
    #             try:
    #                 self._read_frame()
    #             except EOFError:
    #                 # quit when EOF
    #                 break
    #         self._ifile.close()

    ##########################################################################
    # Data modules.
    ##########################################################################

    # Not hashable
    __hash__ = None

    def __init__(self, *, fin=None, fout=None, format=None, store=True, verbose=False,
                            auto=True, extension=True, files=False, nofile=False,
                            ip=False, ipv4=False, ipv6=False, tcp=False, strict=False):
        """Initialise PCAP Reader.

        Keyword arguments:
            * fin  -- str, file name to be read; if file not exist, raise an error
            * fout -- str, file name to be written
            * format  -- str, file format of output
                            <keyword> 'plist' / 'json' / 'tree' / 'html'

            * store -- bool, if store extracted packet info (default is True)
                            <keyword> True / False
            * verbose -- bool, if print verbose output information (default is False)
                            <keyword> True / False

            * auto -- bool, if automatically run till EOF (default is True)
                            <keyword> True / False
            * extension -- bool, if check and append axtensions to output file (default is True)
                            <keyword> True / False

            * files -- bool, if split each frame into different files (default is False)
                            <keyword> True / False
            * nofile -- bool, if no output file is to be dumped (default is False)
                            <keyword> True / False

            * ip -- bool, if record data for IPv4 & IPv6 reassembly (default is False)
                            <keyword> True / False
            * ipv4 -- bool, if perform IPv4 reassembly (default is False)
                            <keyword> True / False
            * ipv6 -- bool, if perform IPv6 reassembly (default is False)
                            <keyword> True / False
            * tcp -- bool, if perform TCP reassembly (default is False)
                            <keyword> True / False

            * strict -- bool, if set strict flag for reassembly (default is False)
                            <keyword> True / False

        """
        ifnm, ofnm, fmt, ext, files = self.make_name(fin, fout, format, extension, files=files, nofile=nofile)

        self._ifnm = ifnm               # input file name
        self._ofnm = ofnm               # output file name
        self._fext = ext                # output file extension

        self._flag_a = auto             # auto extract flag
        self._flag_d = store            # store data flag
        self._flag_e = False            # EOF flag
        self._flag_f = files            # split file flag
        self._flag_m = (self._flag_a and CPU_CNT)
                                        # multiprocessing flag
        self._flag_q = nofile           # no output flag
        self._flag_v = verbose          # verbose output flag

        self._frnum = 1                 # frame number
        self._frame = list()            # frame record
        self._reasm = [None] * 3        # frame record for reassembly (IPv4 / IPv6 / TCP)
        self._proto = None              # frame ProtoChain

        self._ipv4 = ipv4 or ip         # IPv4 Reassembly
        self._ipv6 = ipv6 or ip         # IPv6 Reassembly
        self._tcp = tcp                 # TCP Reassembly

        if self._ipv4:
            from jspcap.reassembly.ipv4 import IPv4_Reassembly
            self._reasm[0] = IPv4_Reassembly(strict=strict)
        if self._ipv6:
            from jspcap.reassembly.ipv6 import IPv6_Reassembly
            self._reasm[1] = IPv6_Reassembly(strict=strict)
        if self._tcp:
            from jspcap.reassembly.tcp import TCP_Reassembly
            self._reasm[2] = TCP_Reassembly(strict=strict)

        self._ifile = open(ifnm, 'rb')                      # input file
        if not self._flag_q:
            if fmt == 'plist':
                from jsformat import PLIST as output        # output PLIST file
            elif fmt == 'json':
                from jsformat import JSON as output         # output JSON file
            elif fmt == 'tree':
                from jsformat import Tree as output         # output treeview text file
            elif fmt == 'html':
                from jsformat import JavaScript as output   # output JavaScript file
                fmt = 'js'
            elif fmt == 'xml':
                from jsformat import XML as output          # output XML file
            else:
                raise FormatError(f'Unsupported output format: {fmt}')
            self._ofile = output if self._flag_f else output(ofnm)
                                                            # output file

        if self._flag_m:
            self._mpprc = list()                            # multiprocessing process list
            self._mpfdp = collections.defaultdict(multiprocessing.Queue)
                                                            # multiprocessing file pointer

            self._mpmng = multiprocessing.Manager()         # multiprocessing manager
            self._mpkit = self._mpmng.Namespace()           # multiprocessing utilities

            self._mpkit.counter    = 0                      # work count (on duty)
            self._mpkit.pool       = 1                      # work pool (ready)
            self._mpkit.curent     = 1                      # current frame number
            self._mpkit.eof        = False                  # EOF flag
            self._mpkit.frames     = dict()                 # frame storage
            self._mpkit.reassembly = copy.deepcopy(self._reasm)
                                                            # reassembly buffers

        self._record_header()            # read PCAP global header
        self._record_frames()            # read frames

    def __iter__(self):
        if not self._flag_a:
            return self
        raise IterableError("'Extractor(auto=True)' object is not iterable") 

    def __next__(self):
        try:
            return self._record_frames()
        except EOFError:
            self._ifile.close()
            raise StopIteration

    def __call__(self):
        if not self._flag_a:
            try:
                return self._record_frames()
            except EOFError as error:
                self._ifile.close()
                raise error from None
        raise CallableError("'Extractor(auto=True)' object is not callable")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._ifile.close()

    ##########################################################################
    # Utilities.
    ##########################################################################

    def _aftermathmp(self):
        """Aftermath for multiprocessing."""
        if not self._flag_e and self._flag_m:
            [ proc.join() for proc in self._mpprc ]
            self._frame = [ self._mpkit.frames[x] for x in sorted(self._mpkit.frames) ]
            self._reasm = copy.deepcopy(self._mpkit.reassembly)
            self._mpmng.shutdown()
            for attr in dir(self):
                if re.match('^_mp.*', attr):
                    delattr(self, attr)
            self._frnum -= 1
            # map(lambda attr: delattr(self, attr), filter(lambda attr: re.match('^_mp.*', attr), dir(self)))

    def _update_eof(self):
        """Update EOF flag."""
        self._aftermathmp()
        self._ifile.close()
        self._flag_e = True

    def _record_header(self):
        """Read global header.

         - Extract global header.
         - Make Info object out of header properties.
         - Append Info.
         - Write plist file.

        """
        self._gbhdr = Header(self._ifile)
        self._dlink = self._gbhdr.protocol
        self._vinfo = self._gbhdr.info
        if not self._flag_q:
            if self._flag_f:
                ofile = self._ofile(f'{self._ofnm}/Global Header.{self._fext}')
                ofile(self._gbhdr.info, name='Global Header')
                self._type = ofile.kind
            else:
                self._ofile(self._gbhdr.info, name='Global Header')
                self._type = self._ofile.kind

    def _record_frames(self):
        """Read frames."""
        if self._flag_m:
            self._mpfdp[0].put(self._gbhdr.length)
            self._read_frame()
        else:
            frame = self._extract_frame()
            self._analyse_frame_ng(frame)
            return frame

    def _read_frame(self):
        """Read frames.

         - Extract frames and each layer of packets.
         - Make Info object out of frame properties.
         - Append Info.
         - Write plist & append Info.

        """
        while True:
            # check EOF
            if self._mpkit.eof:     self._update_eof();     break

            # check counter
            if self._mpkit.pool and self._mpkit.counter < CPU_CNT:
                # update file offset
                self._ifile.seek(self._mpfdp.pop(self._frnum-1).get(), os.SEEK_SET)

                # create worker
                # print(self._frnum, 'start')
                proc = multiprocessing.Process(target=self._extract_frame,
                        kwargs={'mpkit': self._mpkit,
                                'mpfdp': self._mpfdp[self._frnum]})

                # update status
                self._mpkit.pool  -= 1
                self._mpkit.counter += 1

                # start and record
                proc.start()
                self._frnum += 1
                self._mpprc.append(proc)

            # check buffer
            if len(self._mpprc) >= CPU_CNT:
                [ proc.join() for proc in self._mpprc[:-4] ]
                del self._mpprc[:-4]

    def _extract_frame(self, *, mpfdp=None, mpkit=None):
        """Extract frame."""
        # check EOF
        if self._flag_e:    raise EOFError

        # extract frame
        if self._flag_m:
            try:
                # extraction
                frame = Frame(self._ifile, num=self._frnum, proto=self._dlink,
                                mpkit=mpkit, mpfdp=mpfdp)
                # analysis
                self._analyse_frame(frame, mpkit=mpkit)
            except EOFError:
                mpkit.eof = True
            finally:
                mpkit.counter -= 1
                self._ifile.close()
                # print(self._frnum, 'done')
        else:
            return Frame(self._ifile, num=self._frnum, proto=self._dlink)

    def _analyse_frame(self, frame, *, mpkit=None):
        """Head quaters for analysis."""
        if self._flag_m:
            # wait until ready
            while mpkit.curent != self._frnum:
                time.sleep(random.randint(0, datetime.datetime.now().second) // 600)

            # analysis and storage
            # print(self._frnum, 'get')
            self._reasm = mpkit.reassembly
            self._analyse_frame_ng(frame, mpkit=mpkit)
            # print(self._frnum, 'analysed')
            mpkit.reassembly = copy.deepcopy(self._reasm)
            # print(self._frnum, 'put')
        else:
            self._analyse_frame_ng(frame)

    def _analyse_frame_ng(self, frame, *, mpkit=None):
        """Analyses frame."""
        # verbose output
        if self._flag_v:
            print(f' - Frame {self._frnum:>3d}: {frame.protochain}')

        # write plist
        frnum = f'Frame {self._frnum}'
        if not self._flag_q:
            if self._flag_f:
                ofile = self._ofile(f'{self._ofnm}/{frnum}.{self._fext}')
                ofile(frame.info, name=frnum)
            else:
                self._ofile(frame.info, name=frnum)

        # record fragments
        if self._tcp:
            self._tcp_reassembly(frame)
        if self._ipv4:
            self._ipv4_reassembly(frame)
        if self._ipv6:
            self._ipv6_reassembly(frame)

        # record frames
        if self._flag_m:
            if self._flag_d:
                frame._file = NotImplemented
                mpkit.frames[self._frnum] = copy.deepcopy(frame)
                # print(self._frnum, 'stored')
            mpkit.curent += 1
        else:
            if self._flag_d:
                self._frame.append(frame)
            self._frnum += 1
            self._proto = frame.protochain.chain

    # def _read_frame(self):
    #     """Read frames.

    #     - Extract frames and each layer of packets.
    #     - Make Info object out of frame properties.
    #     - Append Info.
    #     - Write plist & append Info.

    #     """
    #     # read frame header
    #     frame = Frame(self._ifile, num=self._frnum, proto=self._dlink)
    #     if self._flag_v:
    #         print(f' - Frame {self._frnum:>3d}: {frame.protochain}')

    #     # write plist
    #     frnum = f'Frame {self._frnum}'
    #     if not self._flag_q:
    #         if self._flag_f:
    #             ofile = self._ofile(f'{self._ofnm}/{frnum}.{self._fext}')
    #             ofile(frame.info, name=frnum)
    #         else:
    #             self._ofile(frame.info, name=frnum)

    #     # record frames
    #     self._frnum += 1
    #     self._proto = frame.protochain.chain
    #     if self._flag_d:
    #         self._frame.append(frame)

    #     # record fragments
    #     if self._tcp:
    #         self._tcp_reassembly(frame)
    #     if self._ipv4:
    #         self._ipv4_reassembly(frame)
    #     if self._ipv6:
    #         self._ipv6_reassembly(frame)

    #     # return frame record
    #     return frame

    def _ipv4_reassembly(self, frame):
        """Store data for IPv4 reassembly."""
        if 'IPv4' in frame:
            ipv4 = frame['IPv4']
            if ipv4.flags.df:
                return
            data = dict(
                bufid = (
                    ipv4.src,                           # source IP address
                    ipv4.dst,                           # destination IP address
                    ipv4.id,                            # identification
                    ipv4.proto,                         # payload protocol type
                ),
                num = frame.info.number,                # original packet range number
                fo = ipv4.frag_offset,                  # fragment offset
                ihl = ipv4.hdr_len,                     # internet header length
                mf = ipv4.flags.mf,                     # more fragment flag
                tl = ipv4.len,                          # total length, header includes
                header = bytearray(ipv4.packet.header), # raw bytearray type header
                payload = bytearray(ipv4.packet.payload or b''),
                                                        # raw bytearray type payload
            )
            self._reasm[0](data)

    def _ipv6_reassembly(self, frame):
        """Store data for IPv6 reassembly."""
        if 'IPv6' in frame:
            ipv6 = frame['IPv6']
            if 'frag' not in ipv6:
                return
            data = dict(
                bufid = (
                    ipv6.src,                           # source IP address
                    ipv6.dst,                           # destination IP address
                    ipv6.label,                         # label
                    ipv6.ipv6_frag.next,                # next header field in IPv6 Fragment Header
                ),
                num = frame.info.number,                # original packet range number
                fo = ipv6.ipv6_frag.offset,             # fragment offset
                ihl = ipv6.hdr_len,                     # header length, only headers before IPv6-Frag
                mf = ipv6.ipv6_frag.mf,                 # more fragment flag
                tl = ipv6.hdr_len + ipv6.raw_len,       # total length, header includes
                header = bytearray(ipv6.fragment.header),
                                                        # raw bytearray type header before IPv6-Frag
                payload = bytearray(ipv6.fragment.payload or b''),
                                                        # raw bytearray type payload after IPv6-Frag
            )
            self._reasm[1](data)

    def _tcp_reassembly(self, frame):
        """Store data for TCP reassembly."""
        if 'TCP' in frame:
            ip = frame['IPv4'] if 'IPv4' in frame else frame['IPv6']
            tcp = frame['TCP']
            data = dict(
                bufid = (
                    ip.src,                             # source IP address
                    ip.dst,                             # destination IP address
                    tcp.srcport,                        # source port
                    tcp.dstport,                        # destination port
                ),
                num = frame.info.number,                # original packet range number
                ack = tcp.ack,                          # acknowledgement
                dsn = tcp.seq,                          # data sequence number
                syn = tcp.flags.syn,                    # synchronise flag
                fin = tcp.flags.fin,                    # finish flag
                payload = bytearray(tcp.packet.payload or b''),
                                                        # raw bytearray type payload
           )
            raw_len = len(data['payload'])              # payload length, header excludes
            data['first'] = tcp.seq                     # this sequence number
            data['last'] = tcp.seq + raw_len            # next (wanted) sequence number
            data['len'] = raw_len                       # payload length, header excludes
            self._reasm[2](data)