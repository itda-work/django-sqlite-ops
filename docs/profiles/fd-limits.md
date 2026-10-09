# 열린 파일 한도(`nofile`) 가이드

두 배포 프로필([`single-server`](single-server.md)·[`single-server-multiproc`](single-server-multiproc.md))이 함께 쓰는 정본이다. 앱 프로세스는 DB 파일(본체·`-wal`·`-shm`), 클라이언트 소켓(HTTP keep-alive·웹소켓), 로그·라이브러리 파일마다 파일 디스크립터(fd)를 하나씩 쓴다. 동시 연결이 실제로 많은 서비스에서는 이 한도가 곧 수용력이다. 한도에 닿으면 새 `open`·`accept` 가 `EMFILE` 로 실패하고, SQLite 연결은 `unable to open database file` 이 된다.

근거는 **공식 문서**(출처 링크), **재현함**(이 저장소의 랩·호스트에서 실행해 확인), **미검증**으로 나눠 적는다. 재현 환경은 colima 0.10.3(linux/arm64, 커널 6.8.0-117), Docker 29.5.2, Compose 5.5.1 이다(#32, #33).

## 1. 한도의 세 층

| 무엇 | 범위 | 넘으면 | 출처 |
|---|---|---|---|
| `RLIMIT_NOFILE` soft | 프로세스 | 그 프로세스의 `open`·`pipe`·`dup` 등이 `EMFILE` | [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html) |
| `RLIMIT_NOFILE` hard | 프로세스 | soft 의 천장. 권한 없는 프로세스는 soft 를 hard 까지만 올리고, hard 는 내리기만 한다(되돌릴 수 없음). 올리려면 `CAP_SYS_RESOURCE` | [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html) |
| `fs.nr_open` (`/proc/sys/fs/nr_open`) | 커널 | `RLIMIT_NOFILE` 을 올릴 수 있는 최대값. 권한 있는 프로세스에도 적용된다. hard 를 이 값보다 크게 올리면 `EPERM`. 기본 1048576 | [proc_sys_fs(5)](https://man7.org/linux/man-pages/man5/proc_sys_fs.5.html), [getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html) |
| `fs.file-max` (`/proc/sys/fs/file-max`) | 시스템 전체 | 모든 프로세스의 열린 파일 합계 한도. 닿으면 `ENFILE`. `CAP_SYS_ADMIN` 프로세스는 넘을 수 있다. 사용량은 `/proc/sys/fs/file-nr` | [proc_sys_fs(5)](https://man7.org/linux/man-pages/man5/proc_sys_fs.5.html) |

- **`nr_open` 과 `file-max` 는 다르다.** `nr_open` 은 프로세스 하나의 한도를 어디까지 **설정할 수 있는가**이고, `file-max` 는 시스템 전체에서 **동시에 열 수 있는 수**다. 컨테이너의 `nofile` 을 올려도 `file-max` 는 그대로다.
- **한도는 물려받는다.** `fork(2)` 의 자식은 부모의 한도를 받고, `execve(2)` 를 지나도 유지된다([getrlimit(2)](https://man7.org/linux/man-pages/man2/getrlimit.2.html)). 프로필의 프로세스 트리(boot → exec → `litestream replicate` → `sh` → `exec uvicorn`)는 그래서 컨테이너에 준 한도를 그대로 쓴다. 재현함: #33 에서 PID 1(`litestream replicate`)과 uvicorn 프로세스의 `/proc/<pid>/limits` 가 모두 compose 로 준 1048576 이었다([장시간 실행 환경](../research/fd-soak-2026-10-09.md#환경)).
- 재현함(colima VM): `fs.nr_open` 1048576, `fs.file-max` 9223372036854775807. compose 에서 hard 를 2097152(> `nr_open`)로 주자 컨테이너가 뜨지 않았다: `error setting rlimits for ready process: error setting rlimit type 7: operation not permitted`(7 = `RLIMIT_NOFILE`).

## 2. 현재 한도 확인

| 방법 | 보는 것 | 출처 |
|---|---|---|
| `cat /proc/<pid>/limits` 의 `Max open files` 줄 | 그 프로세스의 soft·hard(`unlimited` 포함). Linux 2.6.36 부터 모든 사용자가 읽을 수 있다 | [proc_pid_limits(5)](https://man7.org/linux/man-pages/man5/proc_pid_limits.5.html) |
| `ulimit -n` / `ulimit -Hn` | 셸의 soft / hard. `-H`·`-S` 없이 값을 주면 **soft·hard 를 둘 다** 바꾼다. 값 없이 부르면 soft 를 보여 준다(`-H` 면 hard) | bash 5.3 `man bash` 의 `ulimit` |
| `python -c 'import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE))'` | `(soft, hard)`. 무한대는 `resource.RLIM_INFINITY`. `resource` 는 Unix 전용(Windows·WASI 없음) | [Python `resource`](https://docs.python.org/3/library/resource.html) |
| `python manage.py sqlite_doctor` 의 `[process]` | 명령을 실행한 프로세스의 `nofile_soft`·`nofile_hard`([README](../../README.md#sqlite_doctor)) | 이 저장소 |
| `ls /proc/<pid>/fd \| wc -l` | 지금 열려 있는 fd 수. 항목마다 실제 파일을 가리키는 링크다(용량 계산 §4 에 쓴다) | [proc_pid_fd(5)](https://man7.org/linux/man-pages/man5/proc_pid_fd.5.html) |

컨테이너에서는 이렇게 본다(재현함, 출력은 `nofile` soft 4096·hard 8192 를 준 컨테이너).

```bash
docker compose exec app cat /proc/1/limits | grep 'open files'
# Max open files            4096                 8192                 files
docker compose exec app sh -c 'ulimit -n; ulimit -Hn'
# 4096
# 8192
```

- `docker compose exec` 로 들어간 프로세스도 컨테이너의 한도를 받았다(재현함: `/proc/self/limits` 가 `/proc/1/limits` 와 같음). 그래서 `docker compose exec app python manage.py sqlite_doctor` 의 `[process]` 는 앱과 같은 값이다. 다만 앱 프로세스가 스스로 `setrlimit` 으로 한도를 바꿨다면 다르다. 확실히 하려면 앱 PID 의 `/proc/<pid>/limits` 를 본다.
- 재현함: 한도를 주지 않은 컨테이너는 soft 1024·hard 524288 이었다(이 colima VM 의 Docker 29.5.2). 이 기본값은 데몬 설정에서 오므로 호스트마다 다를 수 있다(§3). **배포할 호스트에서 직접 확인한다.**

## 3. 설정 방법

### Docker CLI — `--ulimit`

```bash
docker run --ulimit nofile=4096:8192 ...
```

- 형식은 `<type>=<soft>[:<hard>]`. **hard 를 빼면 soft 값을 둘 다에 쓴다.** 값을 주지 않으면 데몬의 기본 `ulimits` 를 물려받는다([docker run `--ulimit`](https://docs.docker.com/reference/cli/docker/container/run/#ulimit)).
- 데몬 전체의 기본값은 `dockerd --default-ulimit` 또는 `daemon.json` 의 `default-ulimits` 다. 둘 다 없으면 데몬 자신의 한도를 물려받고, `docker run --ulimit` 이 이를 덮는다([dockerd](https://docs.docker.com/reference/cli/dockerd/)).
- 이 플래그 자체는 이 호스트에서 실행하지 않았다(랩 규칙상 compose 로만 띄웠다). 같은 설정을 compose 로 재현했다(아래).

### Docker Compose — `ulimits`

```yaml
services:
  app:
    ulimits:
      nofile:
        soft: 4096
        hard: 8192
```

- 정수 하나(`nofile: 4096`)면 soft·hard 가 같은 값, 매핑이면 `soft`·`hard` 를 따로 준다([Compose `ulimits`](https://docs.docker.com/reference/compose-file/services/#ulimits)).
- 재현함: 매핑 → `/proc/1/limits` 4096·8192, 정수 `4096` → 4096·4096, hard 2097152(> `nr_open`) → 컨테이너 시작 실패(§1). #33 은 이 키로 soft·hard 1048576 을 줬다([장시간 실행 환경](../research/fd-soak-2026-10-09.md#환경)).

### systemd — `LimitNOFILE=`

```ini
[Service]
LimitNOFILE=4096:8192
```

- 값 하나면 soft·hard 를 같은 값으로, `soft:hard` 로 쓰면 따로 정한다. `ulimit -n`·`RLIMIT_NOFILE` 에 해당한다([systemd.exec(5)](https://man7.org/linux/man-pages/man5/systemd.exec.5.html)).
- 지정하지 않으면 `systemd-system.conf` 의 `DefaultLimitNOFILE=` 을 따르고, 그 기본값은 `1024:524288` 이다(systemd 262~devel 판 [systemd-system.conf(5)](https://man7.org/linux/man-pages/man5/systemd-system.conf.5.html)). 설치된 systemd 판의 `man systemd-system.conf` 로 확인한다. 다른 판의 기본값은 여기 적지 않는다.
- systemd.exec(5) 의 주의: Linux 에서 `select(2)` 는 1023 보다 큰 fd 를 다루지 못하므로 soft 를 1024 위로 올릴 때 조심하라고 한다. 이 프로필의 스택(uvicorn·Django·Litestream)은 soft 1048576 에서 열린 fd 1,761개까지 30분간 오류 없이 돌았다(재현함, #33). 다른 라이브러리를 더하면 그것이 `select()` 를 쓰는지 따로 본다.
- systemd 로 띄우는 구성은 이 호스트(macOS)에서 재현하지 않았다(미검증).

## 4. 얼마로 둘까 — 계산법

근거 없는 큰 값(예: 1억)을 권하지 않는다. `nr_open` 을 넘으면 오히려 뜨지 않고(§1), 큰 한도는 누수를 늦게 드러낼 뿐이다(§5). 필요한 값은 앱의 동시 연결 수에서 계산한다.

```text
soft ≥ 평상시 fd + 피크 동시 소켓 수 × 소켓당 fd + DB 연결 수 × 연결당 fd + 여유
hard ≥ soft  (그리고 hard ≤ fs.nr_open)
```

1. **평상시 fd** 를 잰다: 부하가 없을 때 앱 PID 의 `ls /proc/<pid>/fd | wc -l`. 재현함: 프로필 그대로의 uvicorn 프로세스는 부하 전 15–18개(소켓 4개 포함)였다([#33 시계열](../research/fd-soak-2026-10-09.md#시계열-원자료에서-고른-구간) '직전' 줄).
2. **소켓**: 열린 HTTP keep-alive·웹소켓 연결마다 1개. 웹소켓은 접속해 있는 동안 계속 쥔다. 피크 동시 접속 수를 그대로 더한다.
3. **DB**: 권장 설정(`CONN_MAX_AGE` 0)에서 동시 16 요청 30분 동안 10초 구간 180개의 최대가 DB 본체 32개, `-wal` 26개였고, 소켓·기타를 합친 fd 합계는 중앙값 82·최대 90개였다(재현함, [#33 분포](../research/fd-soak-2026-10-09.md#분포와-기울기-180구간-꼬리-제외)의 `0` 열). 동시 수가 다르면 그 수로 같은 측정을 다시 한다. 이 비율을 다른 동시 수로 늘려 쓰는 것은 추정이다.
4. **여유**: 배포 중 겹침, 로그 회전, 관리 명령(`sqlite_doctor` 등)에 쓸 몫이다. 여유를 얼마로 둘지는 측정 근거가 없으므로 정한 값과 이유를 배포 설정 옆에 적어 둔다.

가장 믿을 만한 방법은 **예상 피크 동시 접속으로 부하를 걸고 `ls /proc/<pid>/fd | wc -l` 의 최대값을 잰 뒤** 여유를 더하는 것이다. 열린 fd 는 `/proc/<pid>/fd` 의 링크 대상으로 DB 파일·소켓을 나눠 볼 수 있다(#33 이 쓴 방법, [방법](../research/fd-soak-2026-10-09.md#방법)).

- 기본 soft 1024 로 충분한가: 권장 설정에서 동시 16 은 최대 90개였다(재현함). 웹소켓처럼 연결을 오래 쥐는 접속이 수백 개를 넘으면 위 계산으로 다시 본다.
- 한 머신에 여러 서비스를 띄우면 합계가 `fs.file-max` 안이어야 한다(§1).

## 5. 한도를 올려도 해결되지 않는 것 — `CONN_MAX_AGE=None` + ASGI

ASGI(uvicorn) 의 동기 뷰에서 `CONN_MAX_AGE=None` 은 연결을 재사용하지 않고 닫지도 않아 fd 를 쌓는다. 기본 soft 1024 에서는 DB fd 810–982개에서 `unable to open database file` 500 이 났다([#26 재측정](../research/bench-2026-10-08.md#결과-연결-재사용)). 한도를 1,048,576 으로 올리자(#33, 동시 16·30분):

- 오류는 사라졌다(요청 1,194,162건, 오류 0) — **재현함**.
- 그러나 재사용 이득은 여전히 없고(DB 요청당 연결 생성 1.0, `0` 과 같음) 300초 뒤 평균으로 fd 1,363.0 대 81.4(**16.75배**), 앱 RSS 413.2 대 73.6 MiB(**5.62배**)를 썼다 — **재현함**.
- 부하가 멈춘 뒤에도 `None` 은 DB 본체 fd 996개·`-wal` 215개를 쥐고 있었다(`0` 은 0개) — **재현함**.
- 더 긴 시간·다른 동시 수에서의 정상 상태와, 닫히지 않은 연결을 무엇이 회수하는지는 **미검증**이다([미검증](../research/fd-soak-2026-10-09.md#미검증)).

그래서 이 경우의 대처는 한도가 아니라 설정이다: 일반 별칭의 `CONN_MAX_AGE` 를 `0` 으로 하거나 키를 지운다. 시스템 체크 [`sqlite_ops.W005`](../../README.md#시스템-체크)가 이 설정을 알린다. 한도는 정상 부하(위 §4 계산)를 담기 위해 올린다.

## 6. 프로필 compose 예에 `ulimits` 를 넣지 않은 이유

두 프로필의 `compose.yaml` 예에는 `ulimits` 를 넣지 않았다(#32 판단).

- **기본값으로 충분하다는 근거가 있다.** 프로필 그대로(권장 `CONN_MAX_AGE` 0)의 앱은 동시 16 에서 fd 최대 90개였고(재현함, #33), 회귀 랩 P1·P2 는 한도를 주지 않은 채 통과했다([결과](../research/lab-2026-10-08.md)).
- **넣을 값의 근거가 없다.** 필요한 값은 서비스의 동시 접속 수로 정해진다(§4). 복사해 쓰는 예에 고정 숫자를 넣으면 근거 없는 기본값이 된다.
- **키 자체는 확인했다.** 필요한 서비스는 §3 의 `ulimits` 블록을 `app` 서비스에 더한다. 그 키가 앱 프로세스까지 적용되는 것은 재현했다(§1·§3, #33).

## 7. 미검증

- systemd `LimitNOFILE=` 로 띄운 구성(이 호스트에 systemd 가 없다). `docker run --ulimit` 플래그 직접 실행(compose 의 같은 설정으로만 재현).
- 다른 Docker·containerd 판, 다른 호스트(예: 클라우드 VM)의 기본 `nofile`. §2 의 1024·524288 은 이 colima VM 의 값이다.
- 동시 16 이외의 부하에서 권장 설정의 fd 수(§4 의 비율은 한 측정에서 나온 값).
- `sqlite_doctor` 의 `[process]` 는 보고만 한다. 경고 기준은 근거가 없어 두지 않았다([DESIGN §6-2](../DESIGN.md#6-2-sqlite_doctor-명시-실행-실제-연결)).
