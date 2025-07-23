// 业绩曲线图渲染
function renderPerformanceChart(chartData) {
    const ctx = document.getElementById('performance-chart').getContext('2d');
    const chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: chartData.labels,
            datasets: [
                {
                    label: '策略组合',
                    data: chartData.strategy,
                    borderColor: '#0d6efd',
                    backgroundColor: 'rgba(13, 110, 253, 0.1)',
                    borderWidth: 2,
                    fill: true
                },
                {
                    label: '基准指数',
                    data: chartData.benchmark,
                    borderColor: '#fd7e14',
                    backgroundColor: 'rgba(253, 126, 20, 0.1)',
                    borderWidth: 2,
                    borderDash: [5, 5],
                    fill: false
                }
            ]
        },
        options: {
            responsive: true,
            interaction: {
                mode: 'index',
                intersect: false
            },
            scales: {
                y: {
                    display: true,
                    type: 'logarithmic',
                    title: {
                        display: true,
                        text: '净值(对数刻度)'
                    }
                },
                x: {
                    display: true,
                    title: {
                        display: true,
                        text: '日期'
                    }
                }
            }
        }
    });
}

// 回撤曲线图渲染
function renderDrawdownChart(drawdownData) {
    const ctx = document.getElementById('drawdown-chart').getContext('2d');
    const chart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: drawdownData.labels,
            datasets: [{
                label: '回撤幅度',
                data: drawdownData.values,
                backgroundColor: drawdownData.values.map(val => 
                    val < -0.2 ? '#dc3545' : 
                    val < -0.1 ? '#fd7e14' : '#28a745'
                ),
                borderColor: '#000',
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            scales: {
                y: {
                    display: true,
                    beginAtZero: true,
                    max: 0,
                    min: -0.4,
                    ticks: {
                        callback: value => (value * 100) + '%'
                    }
                },
                x: {
                    display: false
                }
            },
            plugins: {
                legend: {
                    display: false
                }
            }
        }
    });
}