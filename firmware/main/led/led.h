#ifndef _LED_H_
#define _LED_H_

#include <cstdint>

class Led {
public:
    virtual ~Led() = default;
    // Optional direct color controls for addressable RGB indicators.
    virtual bool SupportsColorControl() const { return false; }
    virtual void SetColor(uint8_t r, uint8_t g, uint8_t b) { (void)r; (void)g; (void)b; }
    virtual void TurnOn() {}
    virtual void TurnOff() {}
    virtual bool IsOn() const { return false; }
    // Set the led state based on the device state
    virtual void OnStateChanged() = 0;
};


class NoLed : public Led {
public:
    virtual void OnStateChanged() override {}
};

#endif // _LED_H_
