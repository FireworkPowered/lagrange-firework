import json
import re
from urllib.parse import parse_qsl
from typing import TYPE_CHECKING, TypeVar, Union

from lagrange.client.message.decoder import parse_grp_msg, parse_friend_msg
from lagrange.pb.message.msg_push import MsgPush
from lagrange.pb.status.group import (
    GroupRenamedBody,
    GroupSub16Head,
    MemberChanged,
    MemberGotTitleBody,
    MemberInviteRequest,
    MemberJoinRequest,
    MemberRecallMsg,
    GroupSub20Head,
    PBBotGrayTip,
    PBGroupAlbumUpdate,
    PBGroupBotAdded,
    PBGroupInvite,
    PBSelfJoinInGroup,
)
from lagrange.pb.status.friend import PBFriendRecall, PBFriendRequest
from lagrange.utils.binary.protobuf import proto_decode, ProtoStruct, proto_encode
from lagrange.utils.binary.reader import Reader
from lagrange.utils.operator import unpack_dict, timestamp

from ..events.group import (
    BotGrayTip,
    GroupBotAdded,
    GroupBotJoined,
    GroupInvite,
    GroupMemberGotSpecialTitle,
    GroupMemberJoined,
    GroupMemberJoinRequest,
    GroupMemberQuit,
    GroupMuteMember,
    GroupNameChanged,
    GroupRecall,
    GroupNudge,
    GroupReaction,
    GroupSelfJoined,
    GroupSelfRequireReject,
    GroupSign,
    GroupAlbumUpdate,
    GroupMemberJoinedByInvite,
)
from ..events.friend import FriendRecall, FriendRequest
from ..wtlogin.sso import SSOPacket
from .log import logger

if TYPE_CHECKING:
    from lagrange.client.client import Client

T = TypeVar("T", bound=ProtoStruct)


def unpack(buf2: bytes, decoder: type[T]) -> tuple[int, T]:
    reader = Reader(buf2)
    grp_id = reader.read_u32()
    reader.read_u8()
    return grp_id, decoder.decode(reader.read_bytes_with_length("u16", False))


def parse_quoted_str(string: str) -> list[dict[str, Union[str, int]]]:
    """
    example data: 'test data <{"cmd": 1, "text": "updated"}> <{"cmd": 2}>!'
    to: [{"cmd": 1, "text": "updated"}, {"cmd": 2}]
    """
    return [json.loads(el) for el in re.findall(r"<(\{.*?})>", string)]


async def msg_push_handler(client: "Client", sso: SSOPacket):
    pkg = MsgPush.decode(sso.data).body
    typ = pkg.content_head.type
    sub_typ = pkg.content_head.sub_type

    logger.debug(f"msg_push received, type: {typ}.{sub_typ}")
    if typ == 82:  # grp msg
        return await parse_grp_msg(client, pkg)
    elif typ in [166, 208, 529]:  # frd msg
        if pkg.message:
            return await parse_friend_msg(client, pkg)
    elif typ == 33:  # member joined
        pb = MemberChanged.decode(pkg.message.buf2)
        return GroupMemberJoined(
            grp_id=pb.uin,
            uid=pb.uid,  # right u cant get uin
            join_type=pb.join_type_new,
        )
    elif typ == 34:  # member exit
        pb = MemberChanged.decode(pkg.message.buf2)
        return GroupMemberQuit(
            grp_id=pkg.response_head.from_uin,
            uin=pb.uin,
            uid=pb.uid,
            operator_uid=pb.operator_uid,
            exit_type=pb.exit_type,
        )
    elif typ == 84:
        pb = MemberJoinRequest.decode(pkg.message.buf2)
        return GroupMemberJoinRequest(grp_id=pb.grp_id, uid=pb.uid, answer=pb.request_field)
    elif typ == 85:
        pb = PBSelfJoinInGroup.decode(pkg.message.buf2)
        return GroupSelfJoined(grp_id=pb.gid, op_uid=pb.operator_uid)
    elif typ == 86:
        reader = Reader(pkg.message.buf2)
        grp_id = reader.read_u32()  # it should have more infomation,but i cant guess it
        return GroupSelfRequireReject(grp_id, "")
    elif typ == 87:
        pb = PBGroupInvite.decode(pkg.message.buf2)
        return GroupInvite(grp_id=pb.gid, invitor_uid=pb.invitor_uid)
    elif typ == 167:
        print(pkg.message.encode().hex())
    elif typ == 525:
        pb = MemberInviteRequest.decode(pkg.message.buf2)
        if pb.cmd == 87:
            inn = pb.info.inner
            return GroupMemberJoinRequest(grp_id=inn.grp_id, uid=inn.uid, invitor_uid=inn.invitor_uid)
    elif typ == 0x210:  # friend event, 528 / group file upload notice event
        if sub_typ == 35:  # friend request
            pb = PBFriendRequest.decode(pkg.message.buf2)
            return FriendRequest(
                pkg.response_head.from_uin,
                pb.info.from_uid,
                pkg.response_head.to_uin,
                pb.info.to_uid,
                pb.info.verify,
                pb.info.source,
            )
        elif sub_typ == 138:  # friend recall
            pb = PBFriendRecall.decode(pkg.message.buf2)
            return FriendRecall(
                pkg.response_head.from_uin,
                pb.info.from_uid,
                pkg.response_head.to_uin,
                pb.info.to_uid,
                pb.info.seq,
                pb.info.random,
                pb.info.time,
            )
        if sub_typ == 368:
            pass
            # print(pkg.message.encode().hex())
        logger.debug(f"unhandled friend event / group file upload notice event: {pkg}")  # TODO: paste
    elif typ == 0x2DC:  # grp event, 732
        if sub_typ == 1:
            # print(pkg.encode().hex())
            pass
        if sub_typ == 20:  # nudge and group_sign(群打卡)
            if pkg.message:
                grp_id, pb = unpack(pkg.message.buf2, GroupSub20Head)
                attrs: dict[str, Union[str, int]] = {}
                for x in pb.body.attrs:
                    x: dict[bytes, bytes]
                    k, v = x.values()
                    if isinstance(v, dict):
                        v = proto_encode(v)
                    if v.isdigit():
                        attrs[k.decode()] = int(v.decode())
                    else:
                        attrs[k.decode()] = v.decode()
                if pb.body.f2 == 19217:
                    return GroupBotJoined(
                        grp_id,
                        attrs["mqq_uin"],
                        attrs["nick_name"],
                        attrs["robot_name"],
                        attrs["robot_schema"],
                        attrs["user_schema"],
                    )
                if pb.body.type == 1:
                    if "invitor" in attrs:
                        # reserve: attrs["msg_nums"]
                        return GroupMemberJoinedByInvite(grp_id, attrs["invitor"], attrs["invitee"])
                    elif "user" in attrs and "uin" in attrs:
                        # todo: 群代办
                        pass
                    else:
                        raise TypeError(f"Unhandled GrayTips with attribute: {attrs}")
                elif pb.body.type == 12:
                    return GroupNudge(
                        grp_id,
                        attrs["uin_str1"],
                        attrs["uin_str2"],
                        attrs["action_str"] if "action_str" in attrs else attrs["alt_str1"],  # ?
                        attrs["suffix_str"],
                        attrs,
                        pb.body.attrs_xml,
                    )
                elif pb.body.type == 14:  # grp_sign
                    return GroupSign(
                        grp_id,
                        attrs["mqq_uin"],
                        attrs["mqq_nick"],
                        timestamp(),
                        attrs,
                        pb.body.attrs_xml,
                    )
                else:
                    raise ValueError(f"unknown type({pb.body.type}) f2({pb.body.f2}) on GroupSub20: {attrs}")
            else:
                # print(pkg.encode().hex(), 2)
                return
        elif sub_typ == 16:  # rename & special_title & reaction
            if pkg.message:
                grp_id, pb = unpack(pkg.message.buf2, GroupSub16Head)
                if pb.flag is None:
                    _, pb = unpack(pkg.message.buf2, PBBotGrayTip)  # 傻逼tx，我13号位呢
                    return BotGrayTip(grp_id, pb.body.message)
                if pb.flag == 6:  # special_title
                    body = MemberGotTitleBody.decode(pb.body)
                    for el in parse_quoted_str(body.string):
                        if el["cmd"] == 1:
                            title = el["text"]
                            url = el["data"]
                            break
                    else:
                        raise ValueError("cannot find special_title and url")
                    return GroupMemberGotSpecialTitle(
                        grp_id=grp_id,
                        member_uin=body.member_uin,
                        special_title=title,
                        _detail_url=url,
                    )
                elif pb.flag == 12:  # renamed
                    body = GroupRenamedBody.decode(pb.body)
                    return GroupNameChanged(
                        grp_id=grp_id,
                        name_new=body.grp_name,
                        timestamp=pb.timestamp,
                        operator_uid=pb.operator_uid,
                    )
                elif pb.flag == 35:  # set reaction
                    body = pb.f44.inner.body
                    return GroupReaction(
                        grp_id=grp_id,
                        uid=body.detail.sender_uid,
                        seq=body.msg.id,
                        emoji_id=int(body.detail.emo_id),
                        emoji_type=body.detail.emo_type,
                        emoji_count=body.detail.count,
                        type=body.detail.send_type,
                        total_operations=body.msg.total_operations,
                    )
                elif pb.flag == 23:  # 群幸运字符？
                    pass
                elif pb.flag == 21:  # 位置实时分享（不完整）
                    pass
                    # print(sso.data.hex())
                elif pb.flag == 37:  # 群相册上传（手Q专用:(）
                    _, pb = unpack(pkg.message.buf2, PBGroupAlbumUpdate)  # 塞 就硬塞，可以把你的顾辉盒也给塞进来
                    q = dict(parse_qsl(pb.body.args))
                    return GroupAlbumUpdate(
                        grp_id=pb.grp_id,
                        timestamp=pb.timestamp,
                        image_id=q["i"],
                    )
                elif pb.flag == 38:
                    _, pb = unpack(pkg.message.buf2, PBGroupBotAdded)
                    return GroupBotAdded(pb.body.grp_id, pb.body.bot_uid_1 or pb.body.bot_uid_2)
                else:
                    raise ValueError(f"Unknown subtype_12 flag: {pb.flag}: {pb.body.hex() if pb.body else pb}")
        elif sub_typ == 17:  # recall
            grp_id, pb = unpack(pkg.message.buf2, MemberRecallMsg)

            info = pb.body.info
            return GroupRecall(
                uid=info.uid,
                seq=info.seq,
                time=info.time,
                rand=info.rand,
                grp_id=grp_id,
                suffix=pb.body.extra.suffix.strip() if pb.body.extra else "",
            )
        elif sub_typ == 12:  # mute
            info = proto_decode(pkg.message.buf2)
            return GroupMuteMember(
                grp_id=info.into(1, int),
                operator_uid=info.into(4, bytes).decode(),
                target_uid=unpack_dict(info.proto, "5.3.1", b"").decode(),
                duration=unpack_dict(info.proto, "5.3.2"),
            )
        elif sub_typ == 21:  # set/unset essence msg
            pass  # todo
        else:
            logger.debug(
                "unknown sub_type %d: %s"
                % (
                    sub_typ,
                    pkg.message.buf2.hex() if getattr(pkg.message, "buf2", None) else pkg,
                )
            )
    else:
        logger.debug(
            "unknown type %d, sub type %d: %s"
            % (
                typ,
                sub_typ,
                pkg.message.buf2.hex() if getattr(pkg.message, "buf2", None) else pkg,
            )
        )
