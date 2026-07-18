"""Modals and selects used by the clock views.

These are decoupled from the buttons: each takes a coroutine callback so the
view logic lives in one place (``views.py``) rather than being split across
button/modal classes the way the old file did.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord


class Confirm(discord.ui.View):
    """Simple yes/no confirmation, scoped to a single user."""

    def __init__(self, user: discord.User, timeout: int | None = None):
        super().__init__(timeout=timeout)
        self.user = user
        self.value: bool | None = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        self.value = True
        self.stop()
        await interaction.response.send_message("You clicked Yes!", ephemeral=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.red)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.send_message("You clicked No!", ephemeral=True)


class GetTimeSpent(discord.ui.Modal):
    """Ask for a custom number of hours (quarter-hour increments)."""

    def __init__(self, on_submit: Callable[[discord.Interaction, float], Awaitable[None]]):
        super().__init__(title="Job Completion Form")
        self._on_submit = on_submit
        self.add_item(
            discord.ui.InputText(
                label="Time Spent at Jobsite",
                placeholder="Hours on the quarter hour (e.g. 1, 2.25, 3.5, 4.75)",
                max_length=100,
                style=discord.InputTextStyle.short,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        raw = self.children[0].value
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            await interaction.response.send_message(
                f"You entered a non-numerical answer: {raw}", ephemeral=True
            )
            return
        if hours <= 0 or hours % 0.25 != 0:
            await interaction.response.send_message(
                f"You entered {hours}, which is not on the quarter hour or is not above 0.",
                ephemeral=True,
            )
            return
        await self._on_submit(interaction, hours)


class CustomerInputModal(discord.ui.Modal):
    """Ask for a customer name to search."""

    def __init__(self, on_submit: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(title="Customer Input")
        self._on_submit = on_submit
        self.add_item(
            discord.ui.InputText(
                label="What customer is this work for?",
                placeholder="Enter customer name",
                max_length=100,
                style=discord.InputTextStyle.short,
            )
        )

    async def callback(self, interaction: discord.Interaction):
        await self._on_submit(interaction, self.children[0].value)


class CustomerSelectMenu(discord.ui.Select):
    """Pick a customer from search results."""

    def __init__(self, options, on_select: Callable[[discord.Interaction, int], Awaitable[None]]):
        self._on_select = on_select
        super().__init__(placeholder="Choose a customer", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self._on_select(interaction, int(self.values[0]))
