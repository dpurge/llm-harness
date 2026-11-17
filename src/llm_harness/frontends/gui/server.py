"""Starts the web chat UI: registers the page and runs the NiceGUI server."""

from nicegui import app, ui

from llm_harness.config.settings import HarnessConfig, ModelConfig
from llm_harness.core.subagents import SubagentFactoryMaker
from llm_harness.frontends.gui.chat_page import ChatPage, _open_pages
from llm_harness.frontends.gui.security import add_gui_middleware
from llm_harness.models.router import RouterFactory
from llm_harness.storage import Storage
from llm_harness.tools.registry import ToolRegistry


async def _close_open_pages() -> None:
    """On server shutdown: pages whose client was not deleted yet still own a router."""
    for page in list(_open_pages):
        await page.close()


def register_page(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
    storage: Storage | None = None,
    tools: ToolRegistry | None = None,
    subagent_factory_maker: SubagentFactoryMaker | None = None,
) -> None:
    """Register the chat page; its builder runs once per browser client."""

    @ui.page("/")
    async def chat_page() -> None:
        page = ChatPage(
            config,
            model,
            router_factory,
            is_manual,
            storage,
            tools,
            subagent_factory_maker,
        )
        await page.build()


def run_gui(
    config: HarnessConfig,
    model: ModelConfig,
    router_factory: RouterFactory,
    is_manual: bool,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    show: bool = True,
    storage: Storage | None = None,
    tools: ToolRegistry | None = None,
    subagent_factory_maker: SubagentFactoryMaker | None = None,
) -> int:
    register_page(
        config, model, router_factory, is_manual, storage, tools, subagent_factory_maker
    )
    # Before ui.run: Starlette refuses new middleware once the app has started.
    # NiceGUI mounts socket.io on this same app, so the middleware covers it too.
    add_gui_middleware(app, port, config.gui.a2ui_media_hosts)
    # host is explicit: NiceGUI's own default is 0.0.0.0. reload=False because
    # reload would re-import this module in a child process.
    app.on_shutdown(_close_open_pages)
    if storage is not None:
        # After the pages: their orchestrators may still be using the storage.
        app.on_shutdown(storage.close)
    try:
        ui.run(host=host, port=port, reload=False, show=show, title="llm-harness")
    except KeyboardInterrupt:
        # uvicorn finishes a graceful shutdown and then re-raises Ctrl-C.
        return 130
    return 0
