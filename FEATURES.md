## Features


- The log file in job only shows the recent 300 lines. Give me a functionality that is an option that I can click on to see the details of the log. WHich is the full log in a pure html window is fine, no need to be fancy.

- For the job that runs on gpu or gpu-hp, when they finish. you can use command like 
```
# gpu-hp
slurm-gpu report -r gpu-hp -u $USER -S 2026-11-02 -E 2026-11-04
# gpu
slurm-gpu report -r gpu -u $USER -S 2026-11-02 -E 2026-11-04
```

to get the gpu information. I only need GPUUtil ┆ GPUMemEff from the results. Also show this in each job. For each column. If this is difficult, can you do it when I click on each job it will show me this. 
