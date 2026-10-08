"""``AIOSQLiteChannelLayer._receive_single_from_db`` 의 교체본 (DESIGN §9).

channels-lite 0.4.0 원본(``channels_lite/layers/aio.py`` 143–177)을 그대로 옮기고 선점 판정만
바꿨다. 원본은 ``conn.total_changes > 0`` 으로 판정하는데, 이 값은 연결이 열린 뒤 누적된 변경 수라서
풀에서 재사용된 연결이면 UPDATE 가 0행이어도 참이 된다. 교체본은 UPDATE 커서의 ``rowcount`` 를 본다.

원본과의 차이가 이 두 줄(UPDATE 결과를 ``cursor`` 에 받는 것과 판정)뿐인지는
``tests/test_compat_channels_lite.py`` 가 원본 소스와 대조해 확인한다. 이 파일은 channels-lite 를
import 하므로 ``channels_lite.apply()`` 가 게이트를 통과한 뒤에만 import 한다.
"""

from channels_lite.layers import ChannelEmpty


async def _receive_single_from_db(self, channel):
    """Pull a single message from the database for the given channel."""
    async with self.connection() as conn:
        now = self._to_django_datetime()

        # Find first non-delivered, non-expired message
        cursor = await conn.execute(
            """
            SELECT id, data FROM channels_lite_event
            WHERE channel_name = ? AND delivered = 0 AND expires_at >= ?
            ORDER BY expires_at ASC
            LIMIT 1
            """,
            (channel, now),
        )
        row = await cursor.fetchone()

        if row:
            event_id = row[0]
            data_json = row[1]

            # Mark as delivered
            cursor = await conn.execute(
                "UPDATE channels_lite_event SET delivered = 1 WHERE id = ? AND delivered = 0",
                (event_id,),
            )
            await conn.commit()

            # Check if update was successful
            if cursor.rowcount == 1:
                message = self.deserialize(data_json)
                full_channel = self._extract_message_channel(message, channel)
                return full_channel, message

        raise ChannelEmpty()
