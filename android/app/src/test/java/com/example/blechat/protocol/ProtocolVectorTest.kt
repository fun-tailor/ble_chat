package com.example.blechat.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.UUID

/**
 * 线格式向量，全部由 Python 参考实现（`blechat/protocol.py`）生成。
 *
 * 任何一条不通过 = Kotlin 端跟 PC Host 一定连不上。
 */
class ProtocolVectorTest {

    private val deviceId = UUID.fromString("01234567-89ab-cdef-0123-456789abcdef")
    private val nonceC = hex("101112131415161718191a1b1c1d1e1f")
    private val salt = hex("000102030405060708090a0b0c0d0e0f")
    private val nonceS = hex("202122232425262728292a2b2c2d2e2f")

    // ---------------------------------------------------------------- 控制帧

    @Test
    fun ctrlEncodeMatchesPython() {
        assertEquals("b1010300616263", toHex(encodeCtrl(Ctrl.HELLO, "abc".toByteArray())))
        assertEquals("b10a0300616263", toHex(encodeCtrl(Ctrl.PING, "abc".toByteArray())))
        assertEquals("b10b0300616263", toHex(encodeCtrl(Ctrl.RESYNC, "abc".toByteArray())))
        assertEquals("b10c0000", toHex(encodeCtrl(Ctrl.PONG)))
    }

    @Test
    fun ctrlDecodeMatchesPython() {
        val f = decodeCtrl(hex("b10b0000"))
        assertEquals(Ctrl.RESYNC, f.type)
        assertEquals(0, f.payload.size)
        try {
            decodeCtrl(hex("b0000000"))
            fail("bad magic must fail")
        } catch (e: ProtoException) {
            assertEquals(Err.UNKNOWN, e.code)
        }
    }

    @Test
    fun helloRoundTripMatchesPythonBytes() {
        val h = Hello(deviceId, "设备A", nonceC)
        assertEquals(
            "0123456789abcdef0123456789abcdef07e8aebee5a48741101112131415161718191a1b1c1d1e1f0001",
            toHex(h.encode()),
        )
        val d = Hello.decode(h.encode())
        assertEquals(deviceId, d.deviceId)
        assertEquals("设备A", d.name)
        assertArrayEquals(nonceC, d.nonceC)
        assertFalse(d.useEph)
        assertEquals(P.PROTO_VER, d.protoVer)
    }

    @Test
    fun helloEphemeralRoundTrip() {
        val h = Hello(deviceId, "A", nonceC, useEph = true, ephId = "ABCDEF12")
        assertEquals(
            "0123456789abcdef0123456789abcdef0141101112131415161718191a1b1c1d1e1f01014142434445463132",
            toHex(h.encode()),
        )
        val d = Hello.decode(h.encode())
        assertTrue(d.useEph)
        assertEquals("ABCDEF12", d.ephId)
    }

    @Test
    fun challengeRoundTripMatchesPython() {
        val c = Challenge(nonceS, salt, 1000, useEph = false)
        assertEquals(
            "202122232425262728292a2b2c2d2e2f000102030405060708090a0b0c0d0e0fe803000000",
            toHex(c.encode()),
        )
        val d = Challenge.decode(c.encode())
        assertArrayEquals(nonceS, d.nonceS)
        assertArrayEquals(salt, d.salt)
        assertEquals(1000, d.iters)
        assertFalse(d.useEph)
        try {
            Challenge.decode(ByteArray(35))
            fail("bad length must fail")
        } catch (e: ProtoException) {
            assertEquals(Err.UNKNOWN, e.code)
        }
    }

    @Test
    fun authOkWithHostNameMatchesPython() {
        val a = AuthOk(0x0A0B0C0D, salt, "宿主昵称")
        assertEquals(
            "0d0c0b0a000102030405060708090a0b0c0d0e0f0ce5aebfe4b8bbe698b5e7a7b0",
            toHex(a.encode()),
        )
        val d = AuthOk.decode(a.encode())
        assertEquals(168496141, d.sessionId)
        assertEquals("宿主昵称", d.name)
        assertArrayEquals(salt, d.hkdfSalt)
    }

    @Test
    fun authOkLegacyTwentyBytesStillDecodes() {
        // 老 Host 只发 session_id(4) ‖ hkdf_salt(16)
        val legacy = hex("07000000") + salt
        val d = AuthOk.decode(legacy)
        assertEquals(7, d.sessionId)
        assertEquals("", d.name)
        assertArrayEquals(salt, d.hkdfSalt)
    }

    @Test
    fun ackAndProgressMatchPython() {
        val dead = 0xDEADBEEF.toInt()
        assertEquals("efbeadde0500", toHex(Ack(dead, 5).encode()))
        val a = Ack.decode(hex("efbeadde0500"))
        assertEquals(dead, a.msgId)
        assertEquals(5, a.nextSeq)

        assertEquals("4433221108002800", toHex(Progress(0x11223344, 8, 40).encode()))
        val p = Progress.decode(hex("4433221108002800"))
        assertEquals(0x11223344, p.msgId)
        assertEquals(8, p.acked)
        assertEquals(40, p.total)
    }

    @Test
    fun peersRoundTripMatchesPython() {
        val peers = listOf(
            PeerEntry(deviceId, "设备A"),
            PeerEntry(UUID.fromString("fedcba98-7654-3210-fedc-ba9876543210"), "host"),
        )
        assertEquals(
            "020123456789abcdef0123456789abcdef07e8aebee5a48741fedcba9876543210" +
                "fedcba987654321004686f7374",
            toHex(encodePeers(peers)),
        )
        val back = decodePeers(hex(encodePeersHex(peers)))
        assertEquals(2, back.size)
        assertEquals(deviceId, back[0].deviceId)
        assertEquals("设备A", back[0].name)
        assertEquals("host", back[1].name)
    }

    private fun encodePeersHex(peers: List<PeerEntry>) = toHex(encodePeers(peers))

    // ---------------------------------------------------------------- 数据帧

    @Test
    fun dataChunkHeaderMatchesPython() {
        val c = DataChunk(0x11223344, 2, 3, 1, hex("010203"))
        assertEquals("b244332211020003000109001f361cf1010203", toHex(c.encode()))
        val d = DataChunk.decode(c.encode())
        assertEquals(0x11223344, d.msgId)
        assertEquals(2, d.seq)
        assertEquals(3, d.total)
        assertEquals(1, d.flags)
        assertArrayEquals(hex("010203"), d.chunk)
    }

    @Test
    fun dataChunkCrcCatchesCorruption() {
        val f = DataChunk(0x11223344, 0, 1, 0x05, ByteArray(4)).encode()
        // 头部 = magic(0) ‖ msg_id(1..4) ‖ seq(5..6) ‖ total(7..8) ‖ flags(9) ‖ aad_len(10..11) ‖ crc(12..15)
        val bad = f.copyOf()
        bad[9] = (bad[9].toInt() xor 0xFF).toByte()
        try {
            DataChunk.decode(bad)
            fail("corrupted flags must fail crc")
        } catch (e: ProtoException) {
            assertEquals(Err.CRC_MISMATCH, e.code)
        }
    }

    @Test
    fun chunkSizeMatchesPython() {
        assertEquals(8, P.chunkSize(23))
        assertEquals(8, P.chunkSize(3))
        assertEquals(228, P.chunkSize(247))
        assertEquals(493, P.chunkSize(512))
    }

    @Test
    fun flagsToKind() {
        assertEquals(KIND_TEXT, kindOfFlags(0x01 or 0x04))
        assertEquals(KIND_IMAGE, kindOfFlags(0x02))
        assertEquals(KIND_FILE, kindOfFlags(0x08))
        assertEquals(KIND_FILE, kindOfFlags(0x0A))
    }

    // ---------------------------------------------------------------- 压缩

    @Test
    fun inflatesPythonZlibOutput() {
        val out = Compress.maybeDecompress(hex("789caba8181e0000d0c45dc1"), true)
        assertEquals(200, out.size)
        assertTrue(out.all { it == 'x'.code.toByte() })
    }

    @Test
    fun inflatesPythonZlibOutputOfChineseText() {
        // Python: zlib.compress(("这是一段重复的文本，用来验证压缩。" * 5).encode(), 6)
        val input = "这是一段重复的文本，用来验证压缩。".repeat(5).toByteArray(Charsets.UTF_8)
        assertEquals(255, input.size)
        val z = hex(
            "789c7bb17fe6b319eb9fec6878b66eebcbf6dea74b7a9fcf6a7936adfdd99c35eff" +
                "7f43c9fb2e2d9dca52f57f5bc58dff8b4affbf99e958f1b9a5e0c232d004e02b410",
        )
        assertArrayEquals(input, Compress.maybeDecompress(z, true))
    }

    @Test
    fun nameTruncationMatchesPythonIgnore() {
        // 只有**真的超过 64 字节**时才允许砍掉末尾的半个多字节字符
        val name = "好".repeat(40)               // 120 bytes
        val t = utf8Trunc(name, P.MAX_NAME_LEN)  // = data[:64].decode("utf-8","ignore")
        assertEquals(63, t.size)
        assertEquals("好".repeat(21), String(t, Charsets.UTF_8))
        // 64 字节以内必须原样返回（曾经这里会把最后一个完整字符吃掉）
        val short = utf8Trunc("宿主昵称", P.MAX_NAME_LEN)
        assertEquals("宿主昵称", String(short, Charsets.UTF_8))
        assertArrayEquals("宿主昵称".toByteArray(Charsets.UTF_8), short)
    }

    @Test
    fun smallInputNeverCompressed() {
        val (out, flag) = Compress.maybeCompress("tiny".toByteArray())
        assertFalse(flag)
        assertEquals("tiny", String(out, Charsets.UTF_8))
    }

    @Test
    fun compressibleTextGetsFlaggedAndRoundTrips() {
        val text = "这是一段重复的文本，用来验证压缩。".repeat(20).toByteArray()
        val (out, flag) = Compress.maybeCompress(text)
        assertTrue("expected compression to win", flag)
        assertArrayEquals(text, Compress.maybeDecompress(out, true))
        // 压缩结果必须能被 Python 的 zlib 解（由 Python 脚本二次校验，这里只保证尺寸合理）
        assertTrue(out.size < Compress.GOOD_RATIO * text.size)
    }

    @Test
    fun incompressibleDataKeepsOriginal() {
        val data = ByteArray(256) { (it * 37 + 11).toByte() }
        val (out, flag) = Compress.maybeCompress(data)
        assertFalse(flag)
        assertArrayEquals(data, out)
    }

    @Test
    fun decompressRejectsGarbage() {
        try {
            Compress.maybeDecompress(hex("deadbeef"), true)
            fail("garbage must not decompress")
        } catch (e: ProtoException) {
            assertEquals(Err.UNKNOWN, e.code)
        }
    }

    // ---------------------------------------------------------------- pack / reassemble

    private val pythonPackTextFrames = listOf(
        "b24433221100000200010900480f1e8a47607c26cb5930a0b4618a598053da76bb2ec68caabfba02db3ac4e" +
            "6ef8166e0fe0d925620ae8a0e6b8a1a9fbaad7d83eec60e4d0bc4a2d89fd7bef4b57f5229",
        "b24433221101000200050900e1e213b01b448224",
    )

    private val pythonPackTextPlain = hex(
        "68656c6c6f20424c45206368617420e4b8ade69687e6b58be8af9568656c6c6f20424c45206368617420" +
            "e4b8ade69687e6b58be8af9568656c6c6f20424c45206368617420e4b8ade69687e6b58be8af956865" +
            "6c6c6f20424c45206368617420e4b8ade69687e6b58be8af9568656c6c6f20424c45206368617420e4" +
            "b8ade69687e6b58be8af9568656c6c6f20424c45206368617420e4b8ade69687e6b58be8af9568656c" +
            "6c6f20424c45206368617420e4b8ade69687e6b58be8af9568656c6c6f20424c45206368617420e4b8" +
            "ade69687e6b58be8af95",
    )

    @Test
    fun reassemblesPythonPackedText() {
        val key = ByteArray(32) { it.toByte() }
        val re = Reassembler(key)
        var done: Triple<Int, Int, ByteArray>? = null
        for (f in pythonPackTextFrames) {
            val r = re.feed(hex(f))
            if (r != null) done = r
        }
        val r = done ?: throw AssertionError("did not complete")
        assertEquals(0x11223344, r.first)
        assertEquals(0x01, r.second)
        assertArrayEquals(pythonPackTextPlain, r.third)
        assertEquals(KIND_TEXT, kindOfFlags(r.second))
        assertEquals(0, re.progress(0x11223344))
    }

    @Test
    fun reassemblesPythonPackedImage() {
        val key = ByteArray(32) { it.toByte() }
        val frames = listOf(
            "b20200000000000600020900b6217c224f1aaa34526baf394278eef177a06bc5540bd7d988a075445b4" +
                "6ae7a654e5b86ad130fe3f6896be2586f26e2996dc22c264f4378382b7fa2dc91268d759ce49b372ed" +
                "dbd2d6847da81248b2337b40c9de7ed419ae9dd59f3af73ed3e3bc6c49c17ca1d25",
            "b2020000000100060002090006081c1f1920df5fd726ef2709fe78a3a016fc4e9b7f79f99e5cd3578c7" +
                "99fa8c75145ad3e33c1d2c65c39e7d351665c6e1481f0f5629a5f36af754addc2163f6591b51cd15884" +
                "5adf2be047aa5867c7a9c9abaa4169f3d331a591b0f230f7885e89193137335bdf",
            "b20200000002000600020900d672bc582cf087f6584ae4e3f467098bf95d60365b8f1f9ded792d45d1" +
                "bd1a91451747f5a775da7ceb10bbdb5be7896359d492e6d28fce27a9694f49317b4cab21ec4f8c8ece" +
                "78b79e0d5279b06270498aa02cc179bcb4b8f91fe3bb3200a6d442c6f454baeea1a4",
            "b20200000003000600020900665bdc65069f2c0f6a1593f6b1299226aa67379655cb916fa139324782" +
                "856648dec9893acc42ad7d86babb26de1ca668b801881a34de3bd7a35540a20a7c64a7da487f6f2a28" +
                "8a03fce6937eecb046006ee6730a7696607c730f0fd7cac311773080cfba1572241e",
            "b202000000040006000209007687fcd7768830512094c8119cb59f74975125e6627fd045cee950a2173" +
                "aa30c863308235a4e098952279f174c7a1f35bf4ac9c80c2f9cdd1901cc43db8e5d60f9bddda72da62" +
                "f54e1b55601c814c497b207f32ff81dcbe77c2e5066c4526966bf9906aa46884499",
            "b20200000005000600060900df6af1eddf03c6ca2b37512a1536135b4dc76b55a3cca936f0e42c96ee" +
                "8e9ad9",
        )
        val expected = ByteArray(500) { ((it * 37 + 11) and 0xFF).toByte() }
        val re = Reassembler(key)
        var done: Triple<Int, Int, ByteArray>? = null
        for (f in frames) {
            val r = re.feed(hex(f))
            if (r != null) done = r
        }
        val r = done ?: throw AssertionError("did not complete")
        assertArrayEquals(expected, r.third)
        assertEquals(KIND_IMAGE, kindOfFlags(r.second))
        assertEquals(0x02, r.second and (P.FLAG_IMAGE or P.FLAG_COMPRESSED or P.FLAG_FILE))
    }

    @Test
    fun partialMessageYieldsNullProgress() {
        val key = ByteArray(32) { it.toByte() }
        val re = Reassembler(key)
        assertNull(re.feed(hex(pythonPackTextFrames[0])))
        assertEquals(1, re.progress(0x11223344))
        val r = re.feed(hex(pythonPackTextFrames[1]))
        assertEquals(0x11223344, r!!.first)
    }

    @Test
    fun packProducesCorrectFlagsAndCounts() {
        val key = ByteArray(32) { it.toByte() }
        val plain = pythonPackTextPlain
        val frames = Packer.pack(0x11223344, plain, key, image = false, allowCompress = true, maxChunk = 64)
        assertEquals(2, frames.size)
        val h0 = DataChunk.decode(frames[0])
        val h1 = DataChunk.decode(frames[1])
        assertEquals(0x11223344, h0.msgId)
        assertEquals(0, h0.seq)
        assertEquals(2, h0.total)
        assertEquals(1, h0.flags and P.FLAG_COMPRESSED)
        assertEquals(0, h0.flags and P.FLAG_LAST)
        assertEquals(1, h1.seq)
        assertEquals(P.FLAG_LAST, h1.flags and P.FLAG_LAST)
        // 自己 pack 出来的必须能被自己重组
        val re = Reassembler(key)
        var done: Triple<Int, Int, ByteArray>? = null
        for (f in frames) done = re.feed(f) ?: done
        assertArrayEquals(plain, done!!.third)
    }

    @Test
    fun packSingleFrameSetsLastFlag() {
        val key = ByteArray(32) { it.toByte() }
        val frames = Packer.pack(1, "hi".toByteArray(), key, image = false, allowCompress = true, maxChunk = 200)
        assertEquals(1, frames.size)
        val d = DataChunk.decode(frames[0])
        assertEquals(P.FLAG_LAST, d.flags)
        assertEquals(1, d.total)
        assertEquals(0, d.seq)
        val re = Reassembler(key)
        assertArrayEquals("hi".toByteArray(), re.feed(frames[0])!!.third)
    }

    @Test
    fun packImageSkipsCompressionAndSetsImageFlag() {
        val key = ByteArray(32) { it.toByte() }
        val blob = ByteArray(500) { ((it * 37 + 11) and 0xFF).toByte() }
        val frames = Packer.pack(2, blob, key, image = true, allowCompress = false, maxChunk = 100)
        assertEquals(6, frames.size)
        val first = DataChunk.decode(frames[0])
        assertEquals(0x02, first.flags and P.FLAG_IMAGE)
        assertEquals(0, first.flags and P.FLAG_COMPRESSED)
        val last = DataChunk.decode(frames[5])
        assertEquals(P.FLAG_LAST, last.flags and P.FLAG_LAST)
        val re = Reassembler(key)
        var done: Triple<Int, Int, ByteArray>? = null
        for (f in frames) done = re.feed(f) ?: done
        assertArrayEquals(blob, done!!.third)
    }

    @Test
    fun packRejectsOversizePayload() {
        try {
            Packer.pack(3, ByteArray(P.MAX_PAYLOAD + 1), ByteArray(32), false, true, 100)
            fail("oversize must fail")
        } catch (e: ProtoException) {
            assertEquals(Err.MSG_TOO_LARGE, e.code)
        }
    }

    @Test
    fun wrongKeyCannotReassemble() {
        val frames = Packer.pack(7, "secret".toByteArray(), ByteArray(32) { it.toByte() }, false, true, 200)
        val re = Reassembler(ByteArray(32) { (it + 1).toByte() })
        try {
            re.feed(frames[0])
            fail("wrong key must fail")
        } catch (e: ProtoException) {
            assertEquals(Err.AUTH_FAILED, e.code)
        }
    }

    // ---------------------------------------------------------------- u32 边界

    @Test
    fun u32BitPatternMatchesStructPack() {
        // struct.pack("<I", 0xDEADBEEF) == efbeadde —— 用 Int 的位模式，不要溢出
        assertEquals("efbeadde", toHex(LeWriter().u32(0xDEADBEEF.toInt()).toByteArray()))
        assertEquals("ffffffff", toHex(LeWriter().u32(-1).toByteArray()))
        val r = LeReader(hex("efbeadde"))
        assertEquals(0xDEADBEEF.toInt(), r.u32())
        assertEquals(0, r.remaining())
    }

    @Test
    fun uuidBytesMatchPythonBytes() {
        // str(UUID).bytes 是大端 16 字节，与 ByteBuffer.putLong 的写法一致
        assertEquals("0123456789abcdef0123456789abcdef", toHex(deviceId.toProtoBytes()))
        assertEquals(deviceId, protoBytesToUuid(deviceId.toProtoBytes()))
        assertEquals("01234567-89ab-cdef-0123-456789abcdef", deviceId.toProtocolString())
    }
}
