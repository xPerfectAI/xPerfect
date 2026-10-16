"""Minimal LiveKit worker. Configure speech providers through normal LiveKit model IDs."""
import os

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession
from livekit.agents.types import APIConnectOptions
from livekit.agents.voice.agent_session import SessionConnectOptions
from xperfect_livekit import create_llm

server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext):
    model = create_llm(
        base_url=os.environ["XPERFECT_BASE_URL"],
        api_key=os.environ["XPERFECT_API_KEY"],
        model=os.environ["XPERFECT_MODEL"],
        reasoning_effort=os.getenv("XPERFECT_EFFORT", "high"),
        max_retries=0,
        conversation_id=f"livekit-{ctx.room.name}",
    )
    ctx.add_shutdown_callback(model.aclose)
    turn_options = {"turn_detection": "stt", "preemptive_generation": {"enabled": False}}
    # Enable only when the selected STT reports authoritative end-of-turn events.
    if os.getenv("LIVEKIT_TRUST_STT_TURN_END", "0") == "1":
        turn_options["endpointing"] = {"min_delay": 0.0}
    session = AgentSession(
        llm=model,
        stt=os.environ["LIVEKIT_STT"],
        tts=os.environ["LIVEKIT_TTS"],
        turn_handling=turn_options,
        aec_warmup_duration=float(os.getenv("LIVEKIT_AEC_WARMUP_SECONDS", "3")),
        conn_options=SessionConnectOptions(llm_conn_options=APIConnectOptions(
            timeout=float(os.getenv("XPERFECT_RESPONSE_TIMEOUT", "120")), max_retry=0,
        )),
    )
    await session.start(
        room=ctx.room,
        agent=Agent(instructions=("You are speaking in a live voice call. Speak ordinary conversational "
                                  "answers; keep internal work silent. Use your configured capabilities.")),
    )
    await ctx.connect()


if __name__ == "__main__":
    agents.cli.run_app(server)
