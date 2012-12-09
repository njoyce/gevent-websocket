import struct

from .exceptions import WebSocketError, FrameTooLargeException
from .python_fixes import is_closed
from .websocket import WebSocket, encode_bytes


__all__ = ['WebSocketHybi']


OPCODE_TEXT   = 0x01
OPCODE_BINARY = 0x02
OPCODE_CLOSE  = 0x08
OPCODE_PING   = 0x09
OPCODE_PONG   = 0x0a


class WebSocketHybi(WebSocket):
    __slots__ = (
        '_chunks',
        'close_code',
        'close_message',
        '_reading'
    )

    def __init__(self, socket, environ):
        super(WebSocketHybi, self).__init__(socket, environ)

        self._chunks = bytearray()
        self.close_code = None
        self.close_message = None
        self._reading = False

    def _parse_header(self, data):
        if len(data) != 2:
            self.close(None)
            raise WebSocketError('Incomplete read while reading header: %r' % data)

        first_byte, second_byte = struct.unpack('!BB', data)

        fin = (first_byte >> 7) & 1
        rsv1 = (first_byte >> 6) & 1
        rsv2 = (first_byte >> 5) & 1
        rsv3 = (first_byte >> 4) & 1
        opcode = first_byte & 0xf

        # frame-fin = %x0 ; more frames of this message follow
        #           / %x1 ; final frame of this message

        # frame-rsv1 = %x0 ; 1 bit, MUST be 0 unless negotiated otherwise
        # frame-rsv2 = %x0 ; 1 bit, MUST be 0 unless negotiated otherwise
        # frame-rsv3 = %x0 ; 1 bit, MUST be 0 unless negotiated otherwise
        if rsv1 or rsv2 or rsv3:
            self.close(1002)
            raise WebSocketError('Received frame with non-zero reserved bits: %r' % str(data))

        if opcode > 0x7 and fin == 0:
            self.close(1002)
            raise WebSocketError('Received fragmented control frame: %r' % str(data))

        if len(self._chunks) > 0 and fin == 0 and not opcode:
            self.close(1002)
            raise WebSocketError('Received new fragment frame with non-zero opcode: %r' % str(data))

        if len(self._chunks) > 0 and fin == 1 and (OPCODE_TEXT <= opcode <= OPCODE_BINARY):
            self.close(1002)
            raise WebSocketError('Received new unfragmented data frame during fragmented message: %r' % str(data))

        has_mask = (second_byte >> 7) & 1
        length = (second_byte) & 0x7f

        # Control frames MUST have a payload length of 125 bytes or less
        if opcode > 0x7 and length > 125:
            self.close(1002)
            raise FrameTooLargeException("Control frame payload cannot be larger than 125 bytes: %r" % str(data))

        return fin, opcode, has_mask, length

    def receive_frame(self):
        """Return the next frame from the socket."""
        fobj = self.fobj

        if fobj is None:
            return

        if is_closed(fobj):
            return

        read = self.fobj.read

        assert not self._reading, 'Reading is not possible from multiple greenlets'
        self._reading = True

        try:
            data0 = read(2)

            if not data0:
                self.close(None)
                return

            fin, opcode, has_mask, length = self._parse_header(data0)

            if not has_mask and length:
                self.close(1002)
                raise WebSocketError('Message from client is not masked')

            if length < 126:
                data1 = ''
            elif length == 126:
                data1 = read(2)

                if len(data1) != 2:
                    self.close()
                    raise WebSocketError('Incomplete read while reading 2-byte length: %r' % (data0 + data1))

                length = struct.unpack('!H', data1)[0]
            else:
                assert length == 127, length
                data1 = read(8)

                if len(data1) != 8:
                    self.close()
                    raise WebSocketError('Incomplete read while reading 8-byte length: %r' % (data0 + data1))

                length = struct.unpack('!Q', data1)[0]

            mask = read(4)
            if len(mask) != 4:
                self.close(None)
                raise WebSocketError('Incomplete read while reading mask: %r' % (data0 + data1 + mask))

            mask = struct.unpack('!BBBB', mask)

            if length:
                payload = read(length)
                if len(payload) != length:
                    self.close(None)
                    args = (length, len(payload))
                    raise WebSocketError('Incomplete read: expected message of %s bytes, got %s bytes' % args)
            else:
                payload = ''

            if payload:
                payload = bytearray(payload)

                for i in xrange(len(payload)):
                    payload[i] = payload[i] ^ mask[i % 4]

            return fin, opcode, payload
        finally:
            self._reading = False
            if self.fobj is None:
                fobj.close()

    def _receive(self):
        """Return the next text or binary message from the socket."""

        opcode = None
        result = bytearray()

        while True:
            frame = self.receive_frame()
            if frame is None:
                if result:
                    raise WebSocketError('Peer closed connection unexpectedly')
                return

            f_fin, f_opcode, f_payload = frame

            if f_opcode in (OPCODE_TEXT, OPCODE_BINARY):
                if opcode is None:
                    opcode = f_opcode
                else:
                    raise WebSocketError('The opcode in non-fin frame is expected to be zero, got %r' % (f_opcode, ))
            elif not f_opcode:
                if opcode is None:
                    self.close(1002)
                    raise WebSocketError('Unexpected frame with opcode=0')
            elif f_opcode == OPCODE_CLOSE:
                if len(f_payload) >= 2:
                    self.close_code = struct.unpack('!H', str(f_payload[:2]))[0]
                    self.close_message = f_payload[2:]
                elif f_payload:
                    self.close(None)
                    raise WebSocketError('Invalid close frame: %s %s %s' % (f_fin, f_opcode, repr(f_payload)))
                code = self.close_code
                if code is None or (code >= 1000 and code < 5000):
                    self.close()
                else:
                    self.close(1002)
                    raise WebSocketError('Received invalid close frame: %r %r' % (code, self.close_message))
                return
            elif f_opcode == OPCODE_PING:
                self.send_frame(f_payload, opcode=OPCODE_PONG)
                continue
            elif f_opcode == OPCODE_PONG:
                continue
            else:
                self.close(None)  # XXX should send proper reason?
                raise WebSocketError("Unexpected opcode=%r" % (f_opcode, ))

            result.extend(f_payload)
            if f_fin:
                break

        if opcode == OPCODE_TEXT:
            return result, False
        elif opcode == OPCODE_BINARY:
            return result, True
        else:
            raise AssertionError('internal serror in gevent-websocket: opcode=%r' % (opcode, ))

    def receive(self):
        result = self._receive()
        if not result:
            return result

        message, is_binary = result
        if is_binary:
            return message
        else:
            try:
                return message.decode('utf-8')
            except ValueError:
                self.close(1007)
                raise

    def send_frame(self, message, opcode):
        """Send a frame over the websocket with message as its payload"""

        if self.socket is None:
            raise WebSocketError('The connection was closed')

        header = chr(0x80 | opcode)

        if isinstance(message, unicode):
            message = message.encode('utf-8')

        msg_length = len(message)

        if msg_length < 126:
            header += chr(msg_length)
        elif msg_length < (1 << 16):
            header += chr(126) + struct.pack('!H', msg_length)
        elif msg_length < (1 << 63):
            header += chr(127) + struct.pack('!Q', msg_length)
        else:
            raise FrameTooLargeException()

        try:
            combined = header + message
        except TypeError:
            with self._writelock:
                self._write(header)
                self._write(message)
        else:
            with self._writelock:
                self._write(combined)

    def send(self, message, binary=None):
        """Send a frame over the websocket with message as its payload"""
        if binary is None:
            binary = not isinstance(message, (str, unicode))

        if binary:
            return self.send_frame(message, OPCODE_BINARY)
        else:
            return self.send_frame(message, OPCODE_TEXT)

    def close(self, code=1000, message=''):
        """
        Close the websocket, sending the specified code and message.

        Set `code` to None if you just want to sever the connection.
        """
        if not self.socket:
            # already closing/closed.
            return

        self.socket = None

        if not code:
            super(WebSocketHybi, self).close()

            return

        try:
            message = encode_bytes(message)

            self.send_frame(
                struct.pack('!H%ds' % len(message), code, message),
                opcode=OPCODE_CLOSE)
        except WebSocketError:
            # failed to write the closing frame but its ok because we're
            # closing the socket anyway.
            pass
        finally:
            super(WebSocketHybi, self).close()
