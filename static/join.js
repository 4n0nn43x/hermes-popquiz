// Join page: room code + nickname, then the play page does the WebSocket join.
import { $, roomFromUrl, store } from "./common.js";

const roomInput = $("#room");
const nickInput = $("#nick");
roomInput.value = roomFromUrl();
nickInput.value = store.get("popquiz.nick") || "";
(roomInput.value ? nickInput : roomInput).focus();

$("#join-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const room = roomInput.value.trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  const nick = nickInput.value.trim().replace(/\s+/g, " ");
  if (room.length < 4) { $("#err").textContent = "Check the room code on the big screen"; return; }
  if (!nick || nick.length > 24) { $("#err").textContent = "Pick a nickname (1 to 24 characters)"; return; }
  store.set("popquiz.nick", nick);
  const q = new URLSearchParams({ room, nick });
  location.assign("/play?" + q.toString());
});
