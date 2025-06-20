# server.py
# -*- coding: utf-8 -*-
from __future__ import print_function, unicode_literals
import threading
import socket
from datetime import datetime
import six
from gevent import monkey; monkey.patch_all()
import gevent
from gevent.timeout import Timeout
from gevent.queue import Queue

from package.player import Player
from package.game import Game, ToolCard

try:
    import SocketServer  # Python 2
except ImportError:
    import socketserver as SocketServer  # Python 3


def format_log(msg):
    """回傳帶時間戳的 log 字串。"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    return u"[{}] {}".format(timestamp, msg)


class ConnectionManager(object):
    """
    負責所有網路 I/O：
      - 接受新連線
      - 啟動「讀取指令」協程與「心跳檢測」協程
      - 斷線時通知遊戲主持
    """
    def __init__(self, host, port):
        # 建立 listener socket
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, port))
        self.listener.listen(5)

        self.players = []  # 存放 Player 物件
        self.start_event = threading.Event()  # 兩人連線後觸發
        self.end_event = threading.Event()    # 遊戲結束後觸發

        # 保護 players 清單的鎖，防止同時多個 handler 產生 race condition
        self._lock = threading.Lock()

    def serve_forever(self):
        """
        不斷 accept 新連線，為每位玩家建立 Player 物件，
        並啟動兩條綠線程：_cmd_reader、_heartbeat。
        """
        while True:
            try:
                client_sock, client_addr = self.listener.accept()
            except Exception:
                break  # 可能 listener 被關了
            with self._lock:
                if len(self.players) >= 2:
                    client_sock.sendall("FULL")
                    continue

                player_id = len(self.players) + 1
            player_obj = Player(u"玩家{}".format(player_id))
            player_obj.socket = client_sock
            player_obj.address = client_addr
            # 兩個佇列：cmd_queue 存一般指令，heartbeat_queue 存心跳回覆
            player_obj.cmd_queue = Queue()
            player_obj.heartbeat_queue = Queue()

            with self._lock:
                self.players.append(player_obj)

            print(format_log(u"%s 已連線" % player_obj.name))

            # 啟動「命令讀取」與「心跳檢測」協程
            gevent.spawn(self._heartbeat, player_obj)
            gevent.spawn(self._cmd_reader, player_obj)

            # 如果剛好是第 2 個玩家，觸發 start_event
            with self._lock:
                if len(self.players) == 2:
                    self.start_event.set()

    def _cmd_reader(self, player):
        """
        永遠從 player.socket.recv() 讀資料：
          - 如果讀到 ""，表示對方優雅關閉 → 推入 {'type': 'DISCONNECT'}
          - 如果讀到 "HEARTBEAT_ACK"，推到 heartbeat_queue
          - 否則，推到 cmd_queue，交由遊戲主持處理
        """
        sock = player.socket
        buffer = ""
        while not self.end_event.is_set():
            try:
                data = sock.recv(1024)
            except Exception:
                # recv 出錯視為斷線
                item = {"type": "DISCONNECT"}
                player.cmd_queue.put(item)
                break

            if not data:
                # 客戶端關閉 connection
                item = {"type": "DISCONNECT"}
                player.cmd_queue.put(item)
                break

            buffer += data
            # 以 '\n' 分割每一行
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                text = line.decode("utf-8").strip()
                print(format_log("%s - %s" % (player.name, text)))
                if text == "HEARTBEAT_ACK":
                    player.heartbeat_queue.put(True)
                else:
                    player.cmd_queue.put({"type": "COMMAND", "data": text})

    @staticmethod
    def send_to(player, msg):
        msg = msg if isinstance(msg, unicode) else msg.encode("utf-8")
        try:
            player.socket.sendall(msg.encode("utf-8"))
        except socket.error as e:
            print(format_log("%s - %s" % (player.name, e)))
            player.socket.close()
        except Exception as e:
            print(format_log("%s - %s" % (player.name, e)))

    def _heartbeat(self, player, interval=5, timeout=10):
        """
        每隔 interval 秒發 HEARTBEAT\n，並在 timeout 秒內等待 HEARTBEAT_ACK。
        如果超時或 sendall 失敗，就推入 {'type': 'DISCONNECT'}，結束心跳協程。
        """

        while not self.end_event.is_set():
            try:
                ConnectionManager.send_to(player, "HEARTBEAT\n")
            except Exception:
                # 無法傳送心跳 → 視為斷線
                item = {"type": "DISCONNECT"}
                player.cmd_queue.put(item)
                break
            try:
                with Timeout(timeout):
                    # 等待 HEARTBEAT_ACK 被放入 heartbeat_queue
                    player.heartbeat_queue.get()

            except Exception:
                # 心跳超時或例外 → 視為斷線
                item = {"type": "DISCONNECT"}
                player.cmd_queue.put(item)
                break

            gevent.sleep(interval)

        # 心跳協程結束，close socket
        try:
            player.socket.close()
        except Exception:
            pass

    def shutdown(self):
        """
        在伺服器整體要關閉時呼叫：設置 end_event，並關閉 listener socket。
        """
        self.end_event.set()
        try:
            self.listener.close()
        except Exception:
            pass

    def handle_disconnect(self, current):
        if current in self.players:
            self.players.remove(current)

        if len(self.players) == 1:
            lone = self.players[0]
            ConnectionManager.send_to(lone, "WINNER %s\n" % lone.name)
            self.end_event.set()

class GameHost(object):
    """
    負責遊戲回合流程，從每位 player.cmd_queue.get() 拿指令並執行邏輯。
    斷線時處理勝負並結束遊戲。
    """
    def __init__(self, connection_manager):
        self.connection_manager = connection_manager
        self.players = connection_manager.players

    def broadcast(self, msg, skip_players=None):
        dead = []
        for player in self.players:
            if skip_players is not None and player in skip_players:
                continue
            try:
                ConnectionManager.send_to(player, msg)
            except Exception as e:
                print(format_log("Exception - %s" % e))
                dead.append(player)

        for p in dead:
            self.players.remove(p)

    def reset_turn(self):
        for player in self.players:
            try:
                player.socket.close()
            except Exception:
                pass

        del self.players[:]
        self.connection_manager.start_event.clear()

    @staticmethod
    def save_player_record(player, action):
        """
        儲存玩家丟牌和使用道具的紀錄
        :param player: Player object
        :param action: str
        :return:
        """
        player.guess_histories.append(action)

    def run_game(self):

        # 等待兩位玩家都連線後開始
        self.connection_manager.start_event.wait()
        print(format_log(u"兩位玩家已連線，開始遊戲..."))

        # 建立 Game 物件
        game = Game(self.players)

        # 發初始手牌給所有玩家
        for p in self.players[1:]:
            hand_nums = ",".join(p.number_hand)
            hand_tools = ",".join(p.tool_hand)
            print(format_log("%s - HAND" % p.name))
            ConnectionManager.send_to(p, ("HAND %s;%s\n" % (hand_nums, hand_tools)))

        current_round = 1
        MAX_ROUNDS = game.MAX_ROUNDS

        while current_round <= MAX_ROUNDS:
            # 如果只剩一位玩家，直接宣告勝利
            if len(self.players) == 1:
                lone = self.players[0]
                print(format_log("%s - WINNER" % lone.name))
                ConnectionManager.send_to(lone, "WINNER %s\n" % lone.name)
                self.connection_manager.end_event.set()
                return

            # 依序讓玩家輪流操作
            for idx in [0, 1]:
                # 如果索引超過玩家數量，跳過
                if idx >= len(self.players):
                    break

                current = self.players[idx]
                opponent = self.players[(idx + 1) % len(self.players)]

                # 發送最新手牌
                nums = ",".join(current.number_hand)
                tools = ",".join(current.tool_hand)
                print(format_log("%s - HAND" % current.name))
                ConnectionManager.send_to(current, "HAND %s;%s\n" % (nums, tools))

                # 廣播狀態給對手
                for p in self.players:
                    if p is not current:
                        print(format_log("%s - STATUS" % p.name))
                        ConnectionManager.send_to(p, "STATUS %s\n" % current.name)

                # 道具階段
                print(format_log("%s - TOOL" % current.name))
                ConnectionManager.send_to(current, "TOOL\n")

                try:
                    msg = current.cmd_queue.get()
                except Exception:
                    # 超時或例外 → 斷線
                    self.handle_disconnect(current)
                    return

                if msg["type"] == "DISCONNECTED":
                    self.handle_disconnect(current)
                    return

                extra_guess = False
                if msg["type"] == "COMMAND" and msg["data"].isdigit():
                    ci = int(msg["data"]) - 1
                    if 0 <= ci < len(current.tool_hand):
                        tool = current.tool_hand.pop(ci)
                        print(format_log(u"%s - 使用 %s" % (current.name, tool)))
                        game.discard_tool.append(tool)

                        print(format_log("%s - USED_TOOL" % current.name))
                        ConnectionManager.send_to(current, "USED_TOOL %s\n" % tool)
                        print(format_log("%s - OPP_TOOL" % opponent.name))
                        ConnectionManager.send_to(opponent, "OPP_TOOL %s %s\n" % (current.name, tool))

                        if tool == "POS":
                            # POS 道具處理
                            print(format_log("%s - POS" % current.name))
                            ConnectionManager.send_to(opponent, "POS\n")
                            try:
                                pos_msg = current.cmd_queue.get()
                            except Exception:
                                print(format_log("%s - WINNER" % opponent.name))
                                ConnectionManager.send_to(opponent, "WINNER %s\n" % opponent.name)
                                if current in self.players:
                                    self.players.remove(current)
                                self.connection_manager.end_event.set()
                                return

                            if pos_msg["type"] == "DISCONNECTED":
                                print(format_log("%s - WINNER" % opponent.name))
                                ConnectionManager.send_to(opponent, "WINNER %s\n" % opponent.name)
                                if current in self.players:
                                    self.players.remove(current)
                                self.connection_manager.end_event.set()
                                return

                            pos_str = pos_msg["data"]
                            while not (pos_str.isdigit() and 1 <= int(pos_str) <= game.NUM_GUESS_DIGITS):
                                ConnectionManager.send_to(current, "POS\n")
                                try:
                                    pos_msg = current.cmd_queue.get()
                                except Exception:
                                    print(format_log("%s - WINNER" % opponent.name))
                                    ConnectionManager.send_to(opponent, "WINNER %s\n" % opponent.name)
                                    if current in self.players:
                                        self.players.remove(current)
                                    self.connection_manager.end_event.set()
                                    return

                                if pos_msg["type"] == "DISCONNECTED":
                                    self.handle_disconnect(current)
                                    return

                                pos_str = pos_msg["data"]

                            pi = int(pos_str)
                            digit = ToolCard.pos(opponent.answer, pi)
                            print(format_log("%s - POS_RESULT" % current.name))
                            ConnectionManager.send_to(current, "POS_RESULT %d %s\n" % (pi, digit))

                        elif tool == "SHUFFLE":
                            ToolCard.shuffle(current.answer)
                            print(format_log("%s - SHUFFLE_RESULT" % current.name))
                            ConnectionManager.send_to(current, "SHUFFLE_RESULT %s\n" % "".join(current.answer))

                        elif tool == "EXCLUDE":
                            exclude_result = ToolCard.exclude(opponent.answer)
                            print(format_log("%s - EXCLUDE_RESULT" % current.name))
                            ConnectionManager.send_to(current, "EXCLUDE_RESULT %s\n" % exclude_result)

                        elif tool == "DOUBLE":
                            extra_guess = True
                            print(format_log("%s - DOUBLE_ACTIVE" % current.name))
                            ConnectionManager.send_to(current, "DOUBLE_ACTIVE\n")

                        elif tool == "RESHUFFLE":
                            ToolCard.reshuffle(current.number_hand, game.number_deck)
                            print(format_log("%s - RESHUFFLE_DONE" % current.name))
                            ConnectionManager.send_to(current, "RESHUFFLE_DONE\n")

                # 猜測階段
                guesses = 2 if extra_guess else 1
                for _ in range(guesses):
                    nums = ",".join(current.number_hand)
                    tools = ",".join(current.tool_hand)
                    print(format_log("%s - HAND" % current.name))
                    ConnectionManager.send_to(current, "HAND %s;%s\n" % (nums, tools))
                    print(format_log("%s - GUESS" % current.name))
                    ConnectionManager.send_to(current, "GUESS %s\n" % nums)

                    try:
                        guess_msg = current.cmd_queue.get()
                    except Exception:
                        print(format_log("%s - WINNER" % opponent.name))
                        ConnectionManager.send_to(opponent, "WINNER %s\n" % opponent.name)
                        if current in self.players:
                            self.players.remove(current)
                        self.connection_manager.end_event.set()
                        return

                    if guess_msg["type"] == "DISCONNECTED":
                        self.handle_disconnect(current)
                        return

                    guess = guess_msg["data"]
                    print(format_log(u"%s - 猜了 %s" % (current.name, guess)))
                    for d in guess:
                        current.number_hand.remove(d)
                        game.discard_number.append(d)
                    game.draw_up(current)
                    a, b = game.check_guess(opponent.answer, list(guess))
                    print(format_log("%s - RESULT" % current.name))
                    ConnectionManager.send_to(current, "RESULT %d %d\n" % (a, b))
                    print(format_log("%s - OPP_GUESS" % opponent.name))
                    ConnectionManager.send_to(opponent, "OPP_GUESS %s %s %d %d\n" % (current.name, guess, a, b))

                    if a == game.NUM_GUESS_DIGITS:
                        # 猜中，全部玩家廣播勝利
                        self.broadcast("WINNER %s\n" % current.name)
                        print(format_log("%s - WINNER" % "BROADCAST"))
                        self.reset_turn()
                        return

            current_round += 1

        # 所有回合跑完，沒人猜中 → 平局
        for p in self.players:
            ConnectionManager.send_to(p, "DRAW\n")
        self.reset_turn()


if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 12345
    connection_manager = ConnectionManager(HOST, PORT)

    # 用一條 Thread 啟動 listener
    server_thread = threading.Thread(target=connection_manager.serve_forever)
    server_thread.daemon = True
    server_thread.start()
    print(format_log(u"伺服器已啟動 %s:%d" % (HOST, PORT)))

    game_host = GameHost(connection_manager)

    # 等兩名玩家都連上
    while True:
        game_host.run_game()
