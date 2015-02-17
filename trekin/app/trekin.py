# Copyright (C) 2011 Nippon Telegraph and Telephone Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.topology import switches, event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, ipv4, udp, dhcp, arp
from ryu.lib import addrconv


class SimpleSwitch13(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {'switches': switches.Switches}

    def __init__(self, *args, **kwargs):
        super(SimpleSwitch13, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.dhcp_leases = {}

    @set_ev_cls(event.EventSwitchEnter, MAIN_DISPATCHER)
    def switch_enter(self, ev):
        print "Switch entered"
        datapath = ev.switch.dp
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        print "Datapath: %s" % datapath.id
        # if datapath = 1 then we're awesome
        if datapath.id == 1:
            print "datapath 1 woohoo"
            # punch out dhcp flow
            match = parser.OFPMatch(
                                      eth_type = 0x0800,  # IPv4
                                      ip_proto = 17,      # UDP
                                      udp_dst  = 67      # DHCP request
                                      )
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                              ofproto.OFPCML_NO_BUFFER)]
            self.add_flow(datapath, 10, match, actions)



    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # install table-miss flow entry
        #
        # We specify NO BUFFER to max_len of the output action due to
        # OVS bug. At this moment, if we specify a lesser number, e.g.,
        # 128, OVS will send Packet-In with invalid buffer_id and
        # truncated packet data. In that case, we cannot output packets
        # correctly.  The bug has been fixed in OVS v2.1.0.
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
                                             actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id,
                                    priority=priority, match=match,
                                    instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        # If you hit this you might want to increase
        # the "miss_send_length" of your switch
        if ev.msg.msg_len < ev.msg.total_len:
            self.logger.debug("packet truncated: only %s of %s bytes",
                              ev.msg.msg_len, ev.msg.total_len)
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        dst = eth.dst
        src = eth.src

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})

        self.logger.info("packet in %s %s %s %s", dpid, src, dst, in_port)

        # custom packet handler

        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        if ipv4_pkt:
            print "ipv4 packet"
            udp_pkt = pkt.get_protocol(udp.udp)
            if udp_pkt:
                print "udp packet"
                if udp_pkt.dst_port==67:
                    print "DHCP packet"
                    dhcp_pkt = dhcp.dhcp.parser(pkt.protocols[-1])[0]
                    #print dhcp_pkt
                    self.handle_dhcp(datapath, ofproto, parser, in_port, src, dhcp_pkt)
                    return

        arp_pkt = pkt.get_protocol(arp.arp)

        # learn a mac address to avoid FLOOD next time.
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        # install a flow to avoid packet_in next time
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst)
            # verify if we have a valid buffer_id, if yes avoid to send both
            # flow_mod & packet_out
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, match, actions)
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    # handle_dhcp(datapath, ofproto, parser, in_port, dhcp_pkt)
    def handle_dhcp(self, datapath, ofproto, parser, in_port, src, dhcp_pkt):
        # process packet
        dhcpoptions = [x for x in dhcp_pkt.options.option_list if x.tag == 53]
        if len(dhcpoptions) != 1:
            return
        if dhcpoptions[0].value == '\x01':
            # dhcp discover
            # find request list
            requests = [x for x in dhcp_pkt.options.option_list if x.tag == 55]
            if len(requests) == 1:
                pass
                #print "requesting options:"
                #print ", ".join([str(ord(x)) for x in requests[0].value])
            # look up MAC address in dhcp_leases
            ipaddr = None
            if src in self.dhcp_leases:
                ipaddr = self.dhcp_leases[src]
            else:
                ipaddr = "10.1.1.3" # for giggles
                self.dhcp_leases[src] = ipaddr
            print "Replying to DHCP discover"
            option_list = [
                            dhcp.option(tag=53, value='\x02'),
                            dhcp.option(tag=1, value=addrconv.ipv4.text_to_bin('255.255.255.0')),
                            dhcp.option(tag=51, value='\x00\x00\x21\xc0'),
                            dhcp.option(tag=54, value=addrconv.ipv4.text_to_bin(ipaddr)),
                          ]
            self.dhcp_reply(datapath, ofproto, parser, in_port, src, dhcp_pkt, ipaddr, option_list)

            

        if dhcpoptions[0].value == '\x03':
            print "DHCP request"
            # dhcp request
            requests = [x for x in dhcp_pkt.options.option_list if x.tag == 50]
            if len(requests) != 1:
                print "no option 50"
                return
            if src not in self.dhcp_leases:
                print "no lease for mac %s" % src
                option_list = [
                                dhcp.option(tag=53, value='\x06'),
                              ]
                self.dhcp_reply(datapath, ofproto, parser, in_port, src, dhcp_pkt, '0.0.0.0', option_list)
                return
            reqipaddr = addrconv.ipv4.bin_to_text(requests[0].value)
            ipaddr = self.dhcp_leases[src]
            if ipaddr != reqipaddr:
                print "client requesting wrong address: %s (should be %s)" % (reqipaddr, ipaddr)
                return
            print "Replying to DHCP request"
            option_list = [
                            dhcp.option(tag=53, value='\x05'),
                            dhcp.option(tag=1, value=addrconv.ipv4.text_to_bin('255.255.255.0')),
                            dhcp.option(tag=51, value='\x00\x00\x21\xc0'),
                            dhcp.option(tag=54, value=addrconv.ipv4.text_to_bin(ipaddr)),
                          ]

            self.dhcp_reply(datapath, ofproto, parser, in_port, src, dhcp_pkt, ipaddr, option_list)

    def dhcp_reply(self, datapath, ofproto, parser, in_port, src, dhcp_pkt, ipaddr, option_list):
        # create and send return packet
        options = dhcp.options(option_list)
        dhcp_offer = dhcp.dhcp(op = 2,
                               chaddr = dhcp_pkt.chaddr,
                               hlen = 6,
                               options = options,
                               yiaddr = ipaddr,
                               siaddr = '10.1.1.254',
                               xid = dhcp_pkt.xid)
        # send packet
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=0x0800,
                                            dst=src,
                                            src=0x1234567890))
        pkt.add_protocol(ipv4.ipv4(src='10.1.1.254',
                                    dst='255.255.255.255',
                                    proto=17))
        pkt.add_protocol(udp.udp(src_port=67,
                                  dst_port=68))
        pkt.add_protocol(dhcp_offer)
        pkt.serialize()
        data = pkt.data
        actions = [parser.OFPActionOutput(port=in_port)]
        out = parser.OFPPacketOut(datapath=datapath,
                                  buffer_id=ofproto.OFP_NO_BUFFER,
                                  in_port=ofproto.OFPP_CONTROLLER,
                                  actions=actions,
                                  data=data)
        datapath.send_msg(out)

