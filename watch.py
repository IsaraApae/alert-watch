name: watch
on:
  # Started every 5 minutes by an external scheduler (cron-job.org) calling the dispatch API:
  # GitHub's own `schedule` trigger never fired for this repository.
  workflow_dispatch:
    inputs:
      ping:
        description: Send a test message instead of checking
        type: boolean
        default: false
      richmenu:
        description: Install the Alert Watch menu in the LINE chat
        type: boolean
        default: false
      f1check:
        description: Have LINE check the cards (anime, manga and F1; nothing is sent)
        type: boolean
        default: false
      sites:
        description: Check which anime sites GitHub can read
        type: boolean
        default: false
permissions:
  contents: write
concurrency: watch
jobs:
  watch:
    runs-on: ubuntu-latest
    timeout-minutes: 9  # a stuck run must end before the next one starts
    steps:
      - uses: actions/checkout@v5
      # when the job began, so watch.py knows how long it can wait for an F1 session to start (the job has 9 minutes)
      # (and the OpenVPN cache's name below: the runner's Ubuntu version and this month, so the packages are renewed monthly)
      - run: |
          echo "JOB_START=$(date +%s)" >> "$GITHUB_ENV"
          echo "DEBS_KEY=openvpn-$(. /etc/os-release && echo "$VERSION_ID")-$(date +%Y-%m)" >> "$GITHUB_ENV"
      # the OpenVPN packages vpn_feeds.py installs (debs/), kept between runs so it needn't download them each time
      - uses: actions/cache@v4
        if: ${{ !inputs.ping && !inputs.richmenu && !inputs.f1check }}
        with:
          path: debs
          key: ${{ env.DEBS_KEY }}
      # vpn_feeds.py reads the animeruka feed through a Thai VPN in the background while watch.py does its other
      # checks; watch.py waits for it (feeds/done) only when it gets to that feed. It never fails the job.
      - run: |
          if ${{ !inputs.ping && !inputs.richmenu && !inputs.f1check }}; then
            mkdir -p feeds && rm -f feeds/*
            export FEED_DIR=feeds
            timeout 180 python3 -u vpn_feeds.py &
          fi
          python3 -u watch.py ${{ inputs.ping && 'ping' || '' }}${{ inputs.richmenu && 'richmenu' || '' }}${{ inputs.f1check && 'f1check' || '' }}${{ inputs.sites && 'sites' || '' }} || status=$?
          wait
          exit ${status:-0}
        env:
          MANGA_STATE: ci-state.json
          LINE_TOKEN: ${{ secrets.LINE_TOKEN }}
          YOUTUBE_KEY: ${{ secrets.YOUTUBE_KEY }}
      - run: |
          git config user.name manga-watch
          git config user.email manga-watch@users.noreply.github.com
          git add ci-state.json schedule.json library.json anime.txt thai_times_auto.json
          # the VPN server that last worked (vpn_feeds.py), once there is one
          if [ -f vpn_last.ovpn ]; then git add vpn_last.ovpn; fi
          git diff --cached --quiet && exit 0
          git commit -qm "update state"
          # pull first: LINE commands or another push may have changed the repo. If the same file changed on both sides,
          # keep this run's freshly made version (-X theirs replays our commit on top). A push that loses a race is retried.
          for try in 1 2 3; do
            git pull -q --rebase -X theirs && git push -q && exit 0
            git rebase --abort 2>/dev/null || true
            sleep $((try * 5))
          done
          exit 1
