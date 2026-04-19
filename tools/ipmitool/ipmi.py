from pyghmi.ipmi import command
import traceback

print("=== pyghmi IPMI 测试 ===")

try:
    print("尝试连接 BMC: 172.23.17.208:10623 (cipher_suite=17)")

    ipmi = command.Command(
        bmc="172.23.17.208",
        port=10623,
        userid="Administrator",
        password="Admin@90000",
        cipher_suite=17,
        interface="lanplus",
        keepalive=True,
        timeout=10
    )

    print("连接成功，正在执行 mc info...")

    # 执行 mc info (netfn=0x06, command=0x01)
    result = ipmi.raw_command(netfn=0x06, command=0x01)

    print("\n执行成功！原始返回数据:")
    print(result)

    # 简单解析常见字段
    data = result.get('data', b'')
    if len(data) > 0:
        print("\n解析信息:")
        print(f"  Device ID: {data[0]}")
        print(f"  Device Revision: {data[1]}")
        print(f"  Firmware Revision: {data[2]}.{data[3]}")
        print(f"  IPMI Version: {data[4]}.{data[5]}")
        print(f"  Manufacturer ID: {int.from_bytes(data[6:9], 'little')}")
        print(f"  Product ID: {int.from_bytes(data[9:11], 'little')}")

except Exception as e:
    print("\n发生错误:")
    print(type(e).__name__ + ":", str(e))
    traceback.print_exc()