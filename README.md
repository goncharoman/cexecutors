# cexecutors

Custom Python executors.

<p align="left">
  <a href="https://github.com/goncharoman/cexecutors/actions/workflows/ci.yml?query=branch%3Amain" target="_blank">
    <img src="https://github.com/goncharoman/cexecutors/actions/workflows/ci.yml/badge.svg?branch=main&event=push" alt="CI (main)">
  </a>
  <a href="https://codecov.io/gh/goncharoman/cexecutors" >
    <img src="https://codecov.io/gh/goncharoman/cexecutors/branch/main/graph/badge.svg?token=JRDHLYQXFT"/>
  </a>
</p>

## Instalation

Use git to pull this package:

```bash
git clone https://github.com/goncharoman/cexecutors.git
```

## Usage

### InterruptibleThreadPoolExecutor

```python
import time

from cexecutors import InterruptibleThreadPoolExecutor


def work():
    while True:
        pass


with InterruptibleThreadPoolExecutor() as executor:

    future = executor.submit(work)

    time.sleep(1)
    assert future.running() is True  # future is running

    future.cancel()  # interrupt work func

    time.sleep(1)
    assert future.cancelled() is True
```

## Contributing

Pull requests are welcome.

## License

*Soon...*