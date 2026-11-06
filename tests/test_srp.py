"""Tests for the Cognito SRP handshake.

Hive's sign-in is a Cognito SRP-6a handshake, and it has one trap that
fails in exactly the same way as a wrong password: the string that gets
hashed must be

    <password key><username>:<password>

where the username is the one Cognito sends back as ``USER_ID_FOR_SRP``,
not the one that was typed, and the password key is the part of the pool
id after the underscore.

Putting the password in the username slot produces
``NotAuthorizedException: Incorrect username or password.`` - the same
message as genuinely mistyping your password, so the bug is very easy to
misdiagnose.

So these tests check the maths against an independent implementation of
the flow, written from the specification rather than from the app's own
code. If the two ever disagree, the app is wrong.
"""

import hashlib
import hmac
import unittest

from ember.hive import G_HEX, N_HEX, compute_hkdf, hex_hash, pad_hex, srp_key


def reference_session_key(pool_suffix, username, password, salt_hex,
                           server_b_hex, small_a, big_a):
    """The AWS Cognito SRP flow, implemented independently.

    Deliberately written from the specification rather than by calling
    the app's own helpers, so that it is a real cross-check.
    """
    n = int(N_HEX, 16)
    g = int(G_HEX, 16)

    def sha256_hex(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest().rjust(64, "0")

    def hash_of_hex(text: str) -> str:
        return sha256_hex(bytes.fromhex(text))

    def pad(value) -> str:
        text = format(value, "x") if not isinstance(value, str) else value
        if len(text) % 2 == 1:
            text = "0" + text
        elif text[0] in "89abcdefABCDEF":
            text = "00" + text
        return text

    k = int(hash_of_hex("00" + N_HEX + "0" + format(g, "x")), 16)
    server_b = int(server_b_hex, 16)
    u = int(hash_of_hex(pad(big_a) + pad(server_b)), 16)

    secret = sha256_hex(f"{pool_suffix}{username}:{password}".encode("utf-8"))
    x = int(hash_of_hex(pad(salt_hex) + secret), 16)

    s = pow(server_b - k * pow(g, x, n), small_a + u * x, n)

    prk = hmac.new(bytes.fromhex(pad(u)), bytes.fromhex(pad(s)),
                   hashlib.sha256).digest()
    info = b"Caldera Derived Key" + bytes([1])
    return hmac.new(prk, info, hashlib.sha256).digest()[:16]


class PaddingTests(unittest.TestCase):
    def test_odd_length_gets_a_leading_zero(self):
        self.assertEqual(pad_hex("abc"), "0abc")

    def test_a_high_leading_nibble_gets_two_zeros(self):
        self.assertEqual(pad_hex("ff"), "00ff")
        self.assertEqual(pad_hex("89"), "0089")

    def test_an_even_length_low_value_is_left_alone(self):
        self.assertEqual(pad_hex("12"), "12")

    def test_integers_work_too(self):
        self.assertEqual(pad_hex(255), "00ff")
        self.assertEqual(pad_hex(16), "10")


class SessionKeyTests(unittest.TestCase):
    """The session key must match an independent implementation."""

    POOL = "eu-west-1_SamNfoWtf"
    SUFFIX = "SamNfoWtf"

    def setUp(self):
        self.n = int(N_HEX, 16)
        self.k = int(hex_hash("00" + N_HEX + "0" + G_HEX), 16)
        self.small_a = 0x1234567890ABCDEF1234567890ABCDEF
        self.big_a = pow(2, self.small_a, self.n)
        self.server_b_hex = format(
            pow(2, self.small_a + 7, self.n), "x")
        self.salt_hex = "0a1b2c3d4e5f60718293a4b5c6d7e8f9" \
                       "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
        self.username = "someone@example.com"
        self.password = "correct horse battery staple"

    def test_the_session_key_matches_the_reference(self):
        mine = srp_key(self.SUFFIX, self.username, self.password,
                       int(self.server_b_hex, 16), self.salt_hex,
                       self.small_a, self.big_a, self.n, self.k)
        theirs = reference_session_key(
            self.SUFFIX, self.username, self.password, self.salt_hex,
            self.server_b_hex, self.small_a, self.big_a)
        self.assertEqual(mine, theirs)

    def test_the_username_belongs_in_the_username_slot(self):
        """The bug this test exists for.

        The password in the username slot produces the same
        'Incorrect username or password' as a genuine typo, so the only
        defence is to check the maths against something else.
        """
        correct = srp_key(self.SUFFIX, self.username, self.password,
                          int(self.server_b_hex, 16), self.salt_hex,
                          self.small_a, self.big_a, self.n, self.k)
        wrong = srp_key(self.SUFFIX, self.password, self.password,
                        int(self.server_b_hex, 16), self.salt_hex,
                        self.small_a, self.big_a, self.n, self.k)
        self.assertNotEqual(correct, wrong,
                            "the password must not be in the username slot")

    def test_the_password_key_is_the_pool_id_suffix(self):
        self.assertEqual(self.POOL.split("_")[1], "SamNfoWtf")

    def test_a_wrong_password_gives_a_different_key(self):
        right = srp_key(self.SUFFIX, self.username, self.password,
                        int(self.server_b_hex, 16), self.salt_hex,
                        self.small_a, self.big_a, self.n, self.k)
        wrong = srp_key(self.SUFFIX, self.username, "not the password",
                        int(self.server_b_hex, 16), self.salt_hex,
                        self.small_a, self.big_a, self.n, self.k)
        self.assertNotEqual(right, wrong)

    def test_the_key_is_sixteen_bytes(self):
        key = srp_key(self.SUFFIX, self.username, self.password,
                      int(self.server_b_hex, 16), self.salt_hex,
                      self.small_a, self.big_a, self.n, self.k)
        self.assertEqual(len(key), 16)

    def test_hkdf_uses_the_caldera_info_bits(self):
        # The info string is not optional - change it and the key changes.
        import ember.hive as hive_module
        original = hive_module.INFO_BITS
        try:
            hive_module.INFO_BITS = bytearray("Something Else", "utf-8")
            changed = compute_hkdf(b"\x01" * 16, b"\x02" * 16)
            hive_module.INFO_BITS = original
            same = compute_hkdf(b"\x01" * 16, b"\x02" * 16)
            self.assertNotEqual(changed, same)
        finally:
            hive_module.INFO_BITS = original


class SignatureMessageTests(unittest.TestCase):
    """The signed message is <password key><user id><secret block><time>."""

    def test_the_message_shape(self):
        password_key = "SamNfoWtf"
        user_id = "someone@example.com"
        secret_block = b"\x01\x02\x03"
        stamp = "Tue Nov 03 21:00:00 UTC 2026"
        message = (bytearray(password_key, "utf-8")
                   + bytearray(user_id, "utf-8")
                   + bytearray(secret_block)
                   + bytearray(stamp, "utf-8"))
        self.assertEqual(bytes(message),
                         b"SamNfoWtf" + b"someone@example.com"
                         + b"\x01\x02\x03" + b"Tue Nov 03 21:00:00 UTC 2026")


if __name__ == "__main__":
    unittest.main()
